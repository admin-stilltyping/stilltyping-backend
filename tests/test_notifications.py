from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import test_crm_transactions
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr
from sqlalchemy import select
from test_crm_transactions import (  # noqa: F401
    appointment_body,
    call,
    lead,
    order_body,
    product,
    service,
)

from context_agent import notifications as push
from context_agent.notification_models import Notification, PushDelivery, PushSubscription
from context_agent.support import create_or_get
from super_admin.businesses.models import BusinessAdmin

crm = test_crm_transactions.crm


@pytest.fixture
async def alerts(crm, monkeypatch):
    crm.settings.push_vapid_private_key = SecretStr(push.b64((1).to_bytes(32, "big")))
    crm.settings.push_vapid_subject = "mailto:test@example.com"

    async def deferred(*args):
        pass

    monkeypatch.setattr(push, "dispatch_safe", deferred)
    return crm


def subscription(path="test-device"):
    key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/" + path,
        "keys": {"p256dh": push.b64(key), "auth": push.b64(b"0123456789abcdef")},
    }


async def event(c, kind="order", source=None):
    async with c.db.transaction(c.a.business.slug) as session:
        await push.queue_notification(
            session, c.a.business.id, kind, source or uuid4(), "/orders/" + str(uuid4())
        )


async def deliveries(c):
    async with c.db.transaction() as session:
        return list(await session.scalars(select(PushDelivery)))


async def test_owner_registration_validation_and_no_subscription_secret_disclosure(alerts):
    c = alerts
    assert (await call(c, "GET", "/push/config"))["configured"] is True
    value = subscription()
    first = await call(c, "POST", "/push/subscription", value)
    assert await call(c, "POST", "/push/subscription", value) == first
    assert set(first) == {"id"}
    await call(c, "POST", "/push/subscription", value, owner=c.b, code=409)
    assert await call(c, "GET", f"/push/subscriptions/{first['id']}", owner=c.b) == {
        "active": False
    }
    for endpoint in [
        "http://127.0.0.1/private",
        "https://localhost/private",
        "https://fcm.googleapis.com.attacker.test/a",
        "https://user@fcm.googleapis.com/a",
        "https://fcm.googleapis.com:444/a",
    ]:
        await call(c, "POST", "/push/subscription", {**value, "endpoint": endpoint}, code=400)
    await call(
        c,
        "POST",
        "/push/subscription",
        {**value, "keys": {"p256dh": "bad", "auth": "bad"}},
        code=400,
    )
    other_tenant = await c.client.get(
        f"/admin/{c.b.business.slug}/push/config", headers=c.a.headers
    )
    assert other_tenant.status_code == 403
    assert (await c.client.get(f"/admin/{c.a.business.slug}/notifications")).status_code == 401


async def test_three_requested_creation_events_only_and_retries_are_idempotent(alerts):
    c = alerts
    await call(c, "POST", "/push/subscription", subscription())
    await lead(c)
    p, s = await product(c), await service(c)
    o = order_body(p, contact={"phone": "+919876543210"})
    a = appointment_body(s, contact={"phone": "+919876543210"})
    for _ in range(2):
        await call(c, "POST", "/orders", o, code=201)
        await call(c, "POST", "/appointments", a, code=201)
    request_id = uuid4()
    for _ in range(2):
        async with c.db.transaction(c.a.business.slug) as session:
            await create_or_get(
                session, c.a.business.slug, request_id, "Private patient question", "Private reason"
            )
    history = await call(c, "GET", "/notifications")
    assert len(history) == 3
    assert {n["type"] for n in history} == {"support_ticket", "appointment", "order"}
    assert len(await deliveries(c)) == 3
    assert "Private" not in str(history) and "+919876543210" not in str(history)
    assert await call(c, "GET", "/notifications", owner=c.b) == []
    await call(c, "PATCH", f"/notifications/{history[0]['id']}/read", owner=c.b, code=404)
    await call(c, "PATCH", f"/notifications/{history[0]['id']}/read")
    assert await call(c, "GET", "/notifications/unread-count") == {"count": 2}
    await call(c, "PATCH", "/notifications/read-all", code=204)
    assert await call(c, "GET", "/notifications/unread-count") == {"count": 0}


async def test_only_opted_in_devices_receive_new_committed_events(alerts):
    c = alerts
    await event(c)
    saved = await call(c, "POST", "/push/subscription", subscription())
    await event(c)
    assert len(await deliveries(c)) == 1
    sent = []

    async def sender(settings, device, payload):
        sent.append(payload)
        return 201

    assert await push.dispatch_once(c.db, c.settings, sender=sender) == 1
    assert await push.dispatch_once(c.db, c.settings, sender=sender) == 0
    assert len(sent) == 1 and sent[0]["subscription_id"] == saved["id"]
    assert (await deliveries(c))[0].status == "sent"
    assert not {"endpoint", "keys", "token", "customer"}.intersection(sent[0])


async def test_rollback_does_not_save_alert_or_wake_worker(alerts):
    c = alerts
    wakes = []
    c.db.push_wakeup = lambda: wakes.append(True)
    with pytest.raises(RuntimeError):
        async with c.db.transaction(c.a.business.slug) as session:
            await push.queue_notification(
                session, c.a.business.id, "order", uuid4(), "/orders/test"
            )
            raise RuntimeError("rollback")
    assert await call(c, "GET", "/notifications") == []
    assert wakes == []


async def test_retries_are_backed_off_and_expired_devices_are_removed(alerts):
    c = alerts
    saved = await call(c, "POST", "/push/subscription", subscription())
    await event(c)

    async def unavailable(*args):
        return 429

    await push.dispatch_once(c.db, c.settings, sender=unavailable)
    row = (await deliveries(c))[0]
    assert row.status == "pending" and row.attempts == 1 and row.lease_until is None
    assert await push.dispatch_once(c.db, c.settings, sender=unavailable) == 0
    async with c.db.transaction() as session:
        row = await session.get(PushDelivery, row.id)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    async def gone(*args):
        return 410

    await push.dispatch_once(c.db, c.settings, sender=gone)
    assert await call(c, "GET", f"/push/subscriptions/{saved['id']}") == {"active": False}


@pytest.mark.parametrize("change", ["revoke", "disable", "unsubscribe", "expired", "leased"])
async def test_no_push_to_revoked_or_unsubscribed_accounts_and_no_double_claim(alerts, change):
    c = alerts
    value = subscription()
    saved = await call(c, "POST", "/push/subscription", value)
    await event(c)
    if change == "unsubscribe":
        await call(c, "DELETE", "/push/subscription", {"endpoint": value["endpoint"]}, code=204)
    else:
        async with c.db.transaction() as session:
            device = await session.get(PushSubscription, UUID(saved["id"]))
            account = await session.get(BusinessAdmin, device.admin_id)
            if change == "revoke":
                account.token_version += 1
            elif change == "disable":
                account.is_active = False
            elif change == "expired":
                notification = await session.scalar(select(Notification))
                notification.created_at = datetime.now(UTC) - timedelta(days=2)
            else:
                delivery = await session.scalar(select(PushDelivery))
                delivery.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    sent = []

    async def sender(*args):
        sent.append(True)
        return 201

    await push.dispatch_once(c.db, c.settings, sender=sender)
    assert sent == []


async def test_keys_not_configured_keeps_notification_history_available(alerts):
    c = alerts
    c.settings.push_vapid_private_key = None
    assert await call(c, "GET", "/push/config") == {"configured": False, "public_key": None}
    await call(c, "POST", "/push/subscription", subscription(), code=503)
    await event(c)
    assert len(await call(c, "GET", "/notifications")) == 1
    assert await push.dispatch_once(c.db, c.settings) == 0


async def test_multiple_devices_receive_only_their_own_business(alerts):
    c = alerts
    for name in ["phone", "laptop"]:
        await call(c, "POST", "/push/subscription", subscription(name))
    await call(c, "POST", "/push/subscription", subscription("other-business"), owner=c.b)
    await event(c)
    received = []

    async def sender(settings, device, payload):
        received.append(device["endpoint"])
        return 201

    await push.dispatch_once(c.db, c.settings, sender=sender)
    assert len(received) == 2
    assert not any("other-business" in endpoint for endpoint in received)


async def test_only_commit_triggers_dispatch_after_response_body(alerts, monkeypatch):
    from context_agent.notifications import PushDeliveryMiddleware, after_commit

    c = alerts
    order = []

    async def app(scope, receive, send):
        after_commit(c.db)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    async def dispatched(*args):
        order.append("dispatch")

    async def send(message):
        order.append(message["type"])

    monkeypatch.setattr(push, "dispatch_safe", dispatched)
    from types import SimpleNamespace

    scope = {
        "type": "http",
        "method": "POST",
        "app": SimpleNamespace(
            state=SimpleNamespace(services={"db": c.db, "settings": c.settings})
        ),
    }
    await PushDeliveryMiddleware(app)(scope, None, send)
    assert order == ["http.response.start", "http.response.body", "dispatch"]
    assert push.request_push.get() is None


async def test_sender_encrypts_payload_signs_vapid_and_never_follows_redirects(alerts, monkeypatch):
    import json

    import http_ece
    import requests

    client_key = ec.generate_private_key(ec.SECP256R1())
    point = client_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    secret = b"0123456789abcdef"
    device = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/local-test-only",
        "keys": {"p256dh": push.b64(point), "auth": push.b64(secret)},
    }
    payload = {"title": "New order", "body": "A new order has been placed."}
    requests_seen = []

    def transport(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == 10
        assert kwargs["headers"]["Authorization"].startswith("vapid ")
        decoded = http_ece.decrypt(
            kwargs["data"], private_key=client_key, auth_secret=secret, version="aes128gcm"
        )
        assert json.loads(decoded) == payload
        assert b"New order" not in kwargs["data"]
        requests_seen.append(url)
        response = requests.Response()
        response.status_code = 201
        return response

    monkeypatch.setattr(requests.Session, "request", transport)
    assert await push.send_push(alerts.settings, device, payload) == 201
    assert requests_seen == [device["endpoint"]]
