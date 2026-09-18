"""Persist alerts with the business action; deliver outside its transaction."""

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, or_, select

from super_admin.businesses.models import Business, BusinessAdmin

from .notification_models import Notification, PushDelivery, PushSubscription

log = logging.getLogger(__name__)
request_push = ContextVar("request_push", default=None)
KINDS = {
    "support_ticket": ("New support ticket", "A support ticket needs your attention."),
    "appointment": ("New appointment", "An appointment has been booked."),
    "order": ("New order", "A new order has been placed."),
}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_key(value, length):
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid push encryption key.") from exc
    if len(result) != length:
        raise ValueError("Invalid push encryption key length.")
    return result


def valid_endpoint(value):
    """Only browser-vendor push services, never arbitrary user-provided URLs."""
    parts = urlsplit(value)
    host = parts.hostname or ""
    allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"} or (
        host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
    )
    if (
        not allowed
        or parts.scheme != "https"
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
        or not parts.path.strip("/")
    ):
        raise ValueError("Unsupported browser push endpoint.")
    return value


def vapid(settings):
    secret = getattr(settings, "push_vapid_private_key", None)
    subject = getattr(settings, "push_vapid_subject", "")
    if not secret or not subject:
        return None
    # A raw P-256 private scalar; never a filename supplied to the push library.
    private = decode_key(secret.get_secret_value(), 32)
    if not subject.startswith(("mailto:", "https://")):
        raise ValueError("PUSH_VAPID_SUBJECT must be a contact mailto: or HTTPS URL.")
    return Vapid02(ec.derive_private_key(int.from_bytes(private, "big"), ec.SECP256R1()))


def public_key(settings):
    key = vapid(settings)
    if key is None:
        return None
    return b64(
        key.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )


async def queue_notification(session, business_id, kind, source_id, url):
    title, message = KINDS[kind]
    notification_id = uuid5(NAMESPACE_URL, f"nivaso:notify:{business_id}:{kind}:{source_id}")
    session.add(
        Notification(
            id=notification_id,
            business_id=business_id,
            type=kind,
            title=title,
            message=message,
            url=url,
        )
    )
    # Only devices opted in at event creation receive this alert; no historical flood.
    devices = await session.scalars(
        select(PushSubscription.id)
        .join(BusinessAdmin, BusinessAdmin.id == PushSubscription.admin_id)
        .where(
            PushSubscription.business_id == business_id,
            BusinessAdmin.business_id == business_id,
            BusinessAdmin.is_active.is_(True),
            BusinessAdmin.token_version == PushSubscription.token_version,
        )
    )
    for subscription_id in devices:
        session.add(PushDelivery(notification_id=notification_id, subscription_id=subscription_id))
    session.info["notify_push"] = True


async def queue_support_notification(session, tenant, ticket):
    business_id = await session.scalar(select(Business.id).where(Business.slug == tenant))
    if business_id is not None:
        await queue_notification(
            session, business_id, "support_ticket", ticket.id, f"/support/{ticket.ticket_ref}"
        )


class NoRedirectSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


async def send_push(settings, subscription, payload):
    valid_endpoint(subscription["endpoint"])

    def send():
        with NoRedirectSession() as client:
            response = webpush(
                subscription_info=subscription,
                data=json.dumps(payload),
                vapid_private_key=vapid(settings),
                vapid_claims={"sub": settings.push_vapid_subject},
                ttl=86400,
                timeout=10,
                requests_session=client,
            )
            return response.status_code

    try:
        return await asyncio.to_thread(send)
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 503
    except Exception:
        # SDK exceptions may contain subscription endpoints or encryption keys.
        return 503


async def dispatch_once(db, settings, *, sender=None, limit=20):
    if public_key(settings) is None:
        return 0
    now = datetime.now(UTC)
    sender = sender or send_push
    jobs = []
    async with db.transaction() as session:
        deliveries = list(
            await session.execute(
                select(PushDelivery, Notification, PushSubscription, BusinessAdmin, Business)
                .outerjoin(Notification, Notification.id == PushDelivery.notification_id)
                .outerjoin(PushSubscription, PushSubscription.id == PushDelivery.subscription_id)
                .outerjoin(BusinessAdmin, BusinessAdmin.id == PushSubscription.admin_id)
                .outerjoin(Business, Business.id == PushSubscription.business_id)
                .where(
                    PushDelivery.status == "pending",
                    PushDelivery.next_attempt_at <= now,
                    or_(PushDelivery.lease_until.is_(None), PushDelivery.lease_until < now),
                )
                .order_by(PushDelivery.next_attempt_at, PushDelivery.id)
                .limit(limit)
                .with_for_update(of=PushDelivery, skip_locked=True)
            )
        )
        for delivery, notification, device, account, business in deliveries:
            expired = notification is None or notification.created_at.replace(tzinfo=UTC) < (
                now - timedelta(days=1)
            )
            if (
                expired
                or not device
                or not account
                or not account.is_active
                or account.token_version != device.token_version
                or not business
                or business.status != "active"
                or notification.business_id != business.id
                or account.business_id != business.id
                or delivery.attempts >= 5
            ):
                delivery.status = "cancelled"
                continue
            delivery.attempts += 1
            delivery.lease_id = uuid4()
            delivery.lease_until = now + timedelta(minutes=5)
            jobs.append(
                (
                    delivery.id,
                    delivery.lease_id,
                    {
                        "endpoint": device.endpoint,
                        "keys": {"p256dh": device.p256dh, "auth": device.auth},
                    },
                    {
                        "id": str(notification.id),
                        "subscription_id": str(device.id),
                        "title": notification.title,
                        "body": notification.message,
                        "url": notification.url,
                        "tag": str(notification.id),
                    },
                )
            )
    # All database locks/connections have been released before contacting providers.
    semaphore = asyncio.Semaphore(4)

    async def deliver(job):
        delivery_id, lease_id, subscription, payload = job
        async with semaphore:
            try:
                status = await sender(settings, subscription, payload)
            except Exception:
                status = 503
        return delivery_id, lease_id, status

    results = await asyncio.gather(*(deliver(job) for job in jobs))
    if not results:
        return len(deliveries)
    async with db.transaction() as session:
        rows = {
            row.id: row
            for row in await session.scalars(
                select(PushDelivery)
                .where(PushDelivery.id.in_([r[0] for r in results]))
                .with_for_update()
            )
        }
        expired_devices = []
        for delivery_id, lease_id, status in results:
            row = rows.get(delivery_id)
            if row is None or row.lease_id != lease_id:
                continue
            row.lease_until = None
            row.lease_id = None
            if 200 <= status < 300:
                row.status, row.last_error = "sent", None
            elif status in (404, 410):
                row.status, row.last_error = "expired", "subscription_expired"
                expired_devices.append(row.subscription_id)
            elif row.attempts >= 5 or (400 <= status < 500 and status != 429):
                row.status, row.last_error = "failed", f"provider_{status}"
            else:
                row.last_error = "provider_unavailable"
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30 * 2**row.attempts)
        if expired_devices:
            await session.execute(
                delete(PushSubscription).where(PushSubscription.id.in_(expired_devices))
            )
    return len(deliveries)


def after_commit(db):
    pending = request_push.get()
    if pending is not None:
        pending[0] = True
    elif getattr(db, "push_wakeup", None):
        db.push_wakeup()


async def dispatch_safe(db, settings):
    try:
        if await dispatch_once(db, settings) == 20 and getattr(db, "push_wakeup", None):
            db.push_wakeup()
    except Exception:
        log.error("push_dispatch_failed")


class PushDeliveryMiddleware:
    """Await delivery after the response body, like FastAPI BackgroundTasks.

    This avoids an un-awaited fire-and-forget task on serverless deployments.
    Only a request that committed a new notification triggers a dispatch.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        pending = [False]
        token = request_push.set(pending)
        try:
            await self.app(scope, receive, send)
        finally:
            request_push.reset(token)
            if pending[0]:
                services = scope["app"].state.services
                await dispatch_safe(services["db"], services["settings"])


@asynccontextmanager
async def push_worker(db, settings):
    """Wake after committed events, retry while running, recover on next startup."""
    if public_key(settings) is None:
        yield
        return
    wake = asyncio.Event()
    db.push_wakeup = wake.set

    async def run():
        while True:
            wake.clear()
            try:
                count = await dispatch_once(db, settings)
                if count == 20:
                    continue
            except Exception:
                log.error("push_dispatch_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=60)

    task = asyncio.create_task(run(), name="push-notification-worker")
    try:
        yield
    finally:
        db.push_wakeup = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
