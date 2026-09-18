"""Business-owner APIs; subscription secrets are accepted but never returned."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update

from custom_fields.service import Owner

from .notification_models import Notification, PushSubscription
from .notifications import decode_key, public_key, valid_endpoint
from .schemas import DomainError

router = APIRouter(prefix="/admin/{slug}", tags=["Admin notifications"])


class EndpointInput(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)

    @field_validator("endpoint")
    @classmethod
    def endpoint_allowed(cls, value):
        return valid_endpoint(value)


class PushKeys(BaseModel):
    p256dh: str = Field(max_length=100)
    auth: str = Field(max_length=30)

    @field_validator("p256dh")
    @classmethod
    def public_point(cls, value):
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode_key(value, 65))
        return value

    @field_validator("auth")
    @classmethod
    def auth_secret(cls, value):
        decode_key(value, 16)
        return value


class SubscriptionInput(EndpointInput):
    keys: PushKeys


def output(row):
    return {
        "id": str(row.id),
        "type": row.type,
        "title": row.title,
        "message": row.message,
        "url": row.url,
        "severity": "warning" if row.type == "support_ticket" else "info",
        "is_read": row.read_at is not None,
        "created_at": row.created_at.replace(tzinfo=UTC).isoformat(),
    }


@router.get("/notifications")
async def notifications(
    identity: Owner,
    request: Request,
    unread_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with request.app.state.services["db"].transaction() as session:
        query = select(Notification).where(Notification.business_id == identity.business.id)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        rows = await session.scalars(
            query.order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [output(row) for row in rows]


@router.get("/notifications/unread-count")
async def unread_count(identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        return {
            "count": await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.business_id == identity.business.id, Notification.read_at.is_(None)
                )
            )
        }


@router.patch("/notifications/read-all", status_code=204)
async def read_all(identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        await session.execute(
            update(Notification)
            .where(Notification.business_id == identity.business.id, Notification.read_at.is_(None))
            .values(read_at=datetime.now(UTC))
        )
    return Response(status_code=204)


@router.patch("/notifications/{notification_id}/read")
async def read_one(notification_id: UUID, identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        row = await session.scalar(
            select(Notification).where(
                Notification.id == notification_id, Notification.business_id == identity.business.id
            )
        )
        if row is None:
            raise DomainError(404, "not_found", "Notification not found.")
        row.read_at = row.read_at or datetime.now(UTC)
        await session.flush()
        return output(row)


@router.get("/push/config")
async def push_config(identity: Owner, request: Request):
    key = public_key(request.app.state.services["settings"])
    return {"configured": key is not None, "public_key": key}


@router.post("/push/subscription")
async def subscribe(payload: SubscriptionInput, identity: Owner, request: Request):
    services = request.app.state.services
    if public_key(services["settings"]) is None:
        raise DomainError(503, "push_unavailable", "Browser notifications are not configured yet.")
    endpoint_hash = hashlib.sha256(payload.endpoint.encode()).hexdigest()
    async with services["db"].transaction(identity.business.slug) as session:
        row = await session.scalar(
            select(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash)
        )
        if row and (row.business_id != identity.business.id or row.admin_id != identity.account.id):
            raise DomainError(
                409, "device_in_use", "Disable this device's previous subscription first."
            )
        if row is None:
            count = await session.scalar(
                select(func.count())
                .select_from(PushSubscription)
                .where(PushSubscription.admin_id == identity.account.id)
            )
            if count >= 10:
                raise DomainError(
                    409, "device_limit", "Disable notifications on an older device first."
                )
            row = PushSubscription(
                business_id=identity.business.id,
                admin_id=identity.account.id,
                endpoint_hash=endpoint_hash,
                endpoint=payload.endpoint,
            )
            session.add(row)
        row.token_version = identity.account.token_version
        row.p256dh, row.auth = payload.keys.p256dh, payload.keys.auth
        await session.flush()
        return {"id": str(row.id)}


@router.get("/push/subscriptions/{subscription_id}")
async def subscription_status(subscription_id: UUID, identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        found = await session.scalar(
            select(PushSubscription.id).where(
                PushSubscription.id == subscription_id,
                PushSubscription.admin_id == identity.account.id,
                PushSubscription.business_id == identity.business.id,
                PushSubscription.token_version == identity.account.token_version,
            )
        )
        return {"active": found is not None}


@router.delete("/push/subscription", status_code=204)
async def unsubscribe(payload: EndpointInput, identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        await session.execute(
            delete(PushSubscription).where(
                PushSubscription.endpoint_hash
                == hashlib.sha256(payload.endpoint.encode()).hexdigest(),
                PushSubscription.admin_id == identity.account.id,
                PushSubscription.business_id == identity.business.id,
            )
        )
    return Response(status_code=204)
