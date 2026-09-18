import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.api import create_app
from context_agent.channels import ADAPTERS
from context_agent.config import Settings
from context_agent.db import ChannelAccount
from crm.models import Customer, Enquiry, Lead
from crm.service import capture_incoming
from modules.models import BusinessModules
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business

CREDS = {
    "account_id": "17840000000001",
    "access_token": "test-access-token",
    "app_secret": "test-app-secret",
    "verify_token": "test-verify-token",
}


@pytest.fixture
async def integration(db, monkeypatch):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="instagram-test-only-secret-at-least-32-characters"
    )
    owners = []
    for name in ("Instagram Test A", "Instagram Test B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            modules = await session.get(BusinessModules, business.id)
            modules.leads = True
            admin = await session.scalar(
                select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
            )
        owners.append(
            SimpleNamespace(
                business=business,
                headers={
                    "Authorization": "Bearer "
                    + create_business_token(BusinessIdentity(admin, business), settings)
                },
            )
        )
    calls, sent = [], []

    async def run(tenant, payload):
        calls.append((tenant, payload))
        await capture_incoming(db, tenant, payload)
        return {"answer": "Reply: " + payload.message}

    async def send(config, to, text):
        sent.append((config["account_id"], to, text))

    monkeypatch.setattr(ADAPTERS["instagram"], "send", send)
    services = {"db": db, "settings": settings, "agent": SimpleNamespace(run=run)}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, db=db, a=owners[0], b=owners[1], calls=calls, sent=sent
        )


async def configure(c, owner=None, body=None):
    owner = owner or c.a
    return await c.client.put(
        f"/admin/{owner.business.slug}/channels/instagram",
        headers=owner.headers,
        json=body or CREDS,
    )


async def list_channels(c, owner=None):
    owner = owner or c.a
    return await c.client.get(f"/admin/{owner.business.slug}/channels", headers=owner.headers)


def event(account, sender="sender-1", mid="ig.mid-1", message="Hello"):
    return {
        "id": account,
        "messaging": [
            {
                "sender": {"id": sender},
                "recipient": {"id": account},
                "message": {"mid": mid, "text": message},
            }
        ],
    }


async def deliver(c, entries, secret=CREDS["app_secret"]):
    raw = json.dumps({"object": "instagram", "entry": entries}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return await c.client.post(
        "/webhooks/instagram",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


async def test_instagram_settings_are_persisted_without_echoing_secrets(integration):
    c = integration
    assert (await list_channels(c)).json() == []
    response = await configure(c)
    assert response.status_code == 200, response.text
    expected = {
        "channel_type": "instagram",
        "external_channel_id": CREDS["account_id"],
        "configured": True,
        "is_active": True,
        "webhook_url_path": "/webhooks/instagram",
    }
    assert response.json() == expected
    response = await list_channels(c)
    assert response.json() == [expected]
    assert response.headers["Cache-Control"] == "no-store"
    for name in ("access_token", "app_secret", "verify_token"):
        assert name not in response.text and CREDS[name] not in response.text
    async with c.db.transaction() as session:
        row = await session.scalar(select(ChannelAccount))
        assert row.tenant_id == c.a.business.slug and row.config == CREDS


async def test_update_keeps_existing_secrets_and_changes_only_supplied_values(integration):
    c = integration
    await configure(c)
    response = await configure(
        c,
        body={
            "account_id": CREDS["account_id"],
            "access_token": "new-test-token",
            "app_secret": "",
            "verify_token": None,
        },
    )
    assert response.status_code == 200
    async with c.db.transaction() as session:
        row = await session.scalar(select(ChannelAccount))
        assert row.config == {**CREDS, "access_token": "new-test-token"}
        assert await session.scalar(select(func.count()).select_from(ChannelAccount)) == 1
    assert (await configure(c, body={**CREDS, "account_id": "999"})).status_code == 409


async def test_owner_auth_tenant_isolation_and_duplicate_account(integration):
    c = integration
    url = f"/admin/{c.a.business.slug}/channels"
    for method, path, body in (
        ("GET", url, None),
        ("PUT", url + "/instagram", CREDS),
        ("DELETE", url + "/instagram", None),
    ):
        assert (await c.client.request(method, path, json=body)).status_code == 401
        assert (
            await c.client.request(method, path, json=body, headers=c.b.headers)
        ).status_code == 403
    await configure(c)
    assert (await configure(c, c.b)).status_code == 409
    assert (await list_channels(c, c.b)).json() == []
    async with c.db.transaction() as session:
        row = await session.scalar(select(ChannelAccount))
        assert row.tenant_id == c.a.business.slug


@pytest.mark.parametrize(
    "body",
    [
        {"account_id": "17840000000001"},
        {**CREDS, "account_id": "@username"},
        {**CREDS, "app_secret": " "},
        {**CREDS, "verify_token": ""},
        {**CREDS, "access_token": "x" * 8193},
        {**CREDS, "access_token": "one\ntwo"},
        {**CREDS, "tenant_id": "another-business"},
    ],
)
async def test_invalid_or_incomplete_initial_settings_are_rejected(integration, body):
    c = integration
    assert (await configure(c, body=body)).status_code == 400
    assert (await list_channels(c)).json() == []


async def test_verify_signed_delivery_reply_dedup_and_lead_capture(integration):
    c = integration
    await configure(c)
    response = await c.client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": CREDS["verify_token"],
            "hub.challenge": "challenge",
        },
    )
    assert response.status_code == 200 and response.text == "challenge"
    bad = await c.client.get(
        "/webhooks/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "challenge"},
    )
    assert bad.status_code == 403
    entry = event(CREDS["account_id"])
    assert (await deliver(c, [entry], secret="wrong")).status_code == 401
    assert c.calls == []
    for _ in range(2):
        assert (await deliver(c, [entry])).status_code == 200
    assert len(c.calls) == 1
    assert c.calls[0][0] == c.a.business.slug
    assert c.calls[0][1].channel == "instagram" and c.calls[0][1].external_user_id == "sender-1"
    assert c.sent == [(CREDS["account_id"], "sender-1", "Reply: Hello")]
    async with c.db.transaction() as session:
        for model in (Lead, Enquiry):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert await session.scalar(select(func.count()).select_from(Customer)) == 0


async def test_batched_entries_never_cross_businesses(integration):
    c = integration
    await configure(c)
    await configure(c, c.b, {**CREDS, "account_id": "17840000000002"})
    response = await deliver(
        c,
        [
            event(CREDS["account_id"], mid="mid-a"),
            event("17840000000002", sender="sender-2", mid="mid-b", message="Second business"),
        ],
    )
    assert response.status_code == 200
    assert [(tenant, payload.message) for tenant, payload in c.calls] == [
        (c.a.business.slug, "Hello"),
        (c.b.business.slug, "Second business"),
    ]
    assert [account for account, _, _ in c.sent] == [CREDS["account_id"], "17840000000002"]


async def test_echo_unknown_and_disconnected_accounts_do_not_send(integration):
    c = integration
    await configure(c)
    echo = event(CREDS["account_id"])
    echo["messaging"][0]["message"]["is_echo"] = True
    await deliver(c, [echo, event("unknown")])
    assert c.calls == []
    url = f"/admin/{c.a.business.slug}/channels/instagram"
    for _ in range(2):
        assert (await c.client.delete(url, headers=c.a.headers)).status_code == 204
    assert (await list_channels(c)).json() == []
    assert (await deliver(c, [event(CREDS["account_id"])])).status_code == 200
    assert c.calls == [] and c.sent == []


async def test_invalid_signature_on_later_entry_rejects_whole_batch(integration):
    c = integration
    await configure(c)
    await configure(
        c, c.b, {**CREDS, "account_id": "17840000000002", "app_secret": "other-app-secret"}
    )
    response = await deliver(
        c, [event(CREDS["account_id"]), event("17840000000002", mid="mid-b")]
    )
    assert response.status_code == 401
    assert c.calls == [] and c.sent == []


@pytest.mark.parametrize("raw", [b"not-json", b'{"entry": {}}', b"\xff"])
async def test_malformed_instagram_delivery_is_rejected(integration, raw):
    c = integration
    response = await c.client.post("/webhooks/instagram", content=raw)
    assert response.status_code == 400
    assert c.calls == [] and c.sent == []


async def test_suspended_business_does_not_process_messages(integration):
    c = integration
    await configure(c)
    async with c.db.transaction() as session:
        business = await session.get(Business, c.a.business.id)
        business.status = "suspended"
    assert (await deliver(c, [event(CREDS["account_id"])])).status_code == 200
    assert c.calls == []
    assert (await list_channels(c)).status_code == 401


async def test_telegram_routing_secret_not_exposed_by_channel_list(integration):
    c = integration
    async with c.db.transaction() as session:
        session.add(
            ChannelAccount(
                tenant_id=c.a.business.slug,
                channel="telegram",
                account_id="private-telegram-routing-secret",
                config={
                    "webhook_secret": "private-telegram-routing-secret",
                    "bot_token": "private-bot-token",
                },
            )
        )
    response = await list_channels(c)
    assert response.status_code == 200
    assert "private-" not in response.text
