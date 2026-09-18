"""Business-owned event metadata; credentials and message bodies are never exposed."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from sqlalchemy import case, func, select

from custom_fields.service import Owner

from .ai_usage import utc_iso
from .db import WebhookEvent
from .schemas import DomainError

router = APIRouter(prefix="/admin/{slug}/webhook-events", tags=["Business webhook events"])

DETAILS = {
    "channel_not_configured": "Reply could not be sent because channel credentials are missing. Check Integrations.",
    "channel_authorization_failed": "The channel rejected authorization. Check the access token and permissions in Integrations.",
    "channel_rate_limited": "The channel rate limit prevented sending the reply.",
    "send_timeout": "The send request timed out. Delivery could not be confirmed; no automatic resend was attempted.",
    "send_failed": "The reply was generated, but the channel did not confirm the send request.",
    "generation_failed": "The AI reply could not be completed. Check the knowledge base and AI configuration.",
    "unsupported_message": "Only text messages are supported. This message was recorded without generating a reply.",
    "invalid_message": "The message could not be processed because its text or sender details are invalid.",
    "missing_message_identity": "The message has no usable event ID or sender. No reply was sent.",
    "legacy_duplicate": "This message was already received before detailed tracking started. It was not processed again.",
}
STATUS_DETAILS = {
    "received": "The verified message has been saved and is waiting for processing.",
    "processing": "The message is being processed. A final outcome has not been recorded yet.",
    "processed": "The reply send request succeeded. This does not confirm that the recipient has read it.",
    "failed": "Processing failed. No automatic replay is performed.",
    "ignored": "No AI reply was generated for this event.",
}


@router.get("")
async def list_events(
    identity: Owner,
    request: Request,
    source: Literal["instagram", "whatsapp", "telegram"] | None = None,
    status: Literal["received", "processing", "processed", "failed", "ignored"] | None = None,
    start: date | None = None,
    end: date | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
):
    timezone = identity.business.timezone
    zone = ZoneInfo(timezone)
    today = datetime.now(zone).date()
    start, end = start or today.replace(day=1), end or today
    if end < start or end == date.max:
        raise DomainError(
            400, "invalid_date_range", "Choose an end date on or after the start date."
        )
    conditions = [
        WebhookEvent.tenant_id == identity.business.slug,
        WebhookEvent.created_at >= datetime.combine(start, time.min, zone).astimezone(UTC),
        WebhookEvent.created_at
        < datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    ]
    if source:
        conditions.append(WebhookEvent.channel == source)
    if status:
        conditions.append(WebhookEvent.status == status)
    async with request.app.state.services["db"].transaction() as session:
        totals = (
            await session.execute(
                select(
                    func.count(WebhookEvent.id),
                    func.count(case((WebhookEvent.status == "processed", 1))),
                    func.count(case((WebhookEvent.status == "failed", 1))),
                    func.coalesce(func.sum(WebhookEvent.deliveries - 1), 0),
                ).where(*conditions)
            )
        ).one()
        rows = list(
            await session.scalars(
                select(WebhookEvent)
                .where(*conditions)
                .order_by(WebhookEvent.created_at.desc(), WebhookEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
    return {
        "timezone": timezone,
        "start": start,
        "end": end,
        "total_count": totals[0],
        "next_offset": offset + limit if offset + limit < totals[0] else None,
        "summary": {
            "events": totals[0],
            "replies_sent": totals[1],
            "failed": totals[2],
            "duplicate_deliveries": totals[3],
        },
        "items": [
            {
                "id": row.id,
                "source": row.channel,
                "external_event_id": row.external_event_id,
                "status": row.status,
                "deliveries": row.deliveries,
                "created_at": utc_iso(row.created_at),
                "last_received_at": utc_iso(row.last_received_at) if row.last_received_at else None,
                "completed_at": utc_iso(row.completed_at) if row.completed_at else None,
                "duration_ms": row.duration_ms,
                "request_id": row.request_id,
                "detail": DETAILS.get(
                    row.error_code, STATUS_DETAILS.get(row.status, "Outcome unavailable.")
                ),
            }
            for row in rows
        ],
    }
