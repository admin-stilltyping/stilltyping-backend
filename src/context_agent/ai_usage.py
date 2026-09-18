"""Owner-scoped, per-reply AI usage, filtered in the business's calendar timezone."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from sqlalchemy import case, func, select

from custom_fields.service import Owner

from .db import AiUsageRecord as Usage
from .schemas import DomainError

router = APIRouter(prefix="/admin/{slug}/ai-usage", tags=["Business AI usage"])


def utc_iso(value):
    # SQLite tests return naive UTC; PostgreSQL returns aware timestamptz.
    return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.isoformat()


@router.get("")
async def list_usage(
    identity: Owner,
    request: Request,
    start: date | None = None,
    end: date | None = None,
    channel: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
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
    begin = datetime.combine(start, time.min, zone).astimezone(UTC)
    until = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC)
    conditions = [
        Usage.tenant_id == identity.business.slug,
        Usage.started_at >= begin,
        Usage.started_at < until,
    ]
    if channel:
        conditions.append(Usage.channel == channel)
    async with request.app.state.services["db"].transaction() as session:
        totals = (
            await session.execute(
                select(
                    func.count(Usage.id),
                    func.coalesce(func.sum(Usage.input_tokens), 0),
                    func.coalesce(func.sum(Usage.output_tokens), 0),
                    func.count(case((Usage.tokens_complete.is_(False), 1))),
                    func.avg(case((Usage.status == "completed", Usage.duration_ms))),
                    func.count(case((Usage.status.in_(["failed", "send_failed"]), 1))),
                ).where(*conditions)
            )
        ).one()
        rows = list(
            await session.scalars(
                select(Usage)
                .where(*conditions)
                .order_by(Usage.started_at.desc(), Usage.id.desc())
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
            "replies": totals[0],
            "input_tokens": totals[1],
            "output_tokens": totals[2],
            "incomplete_replies": totals[3],
            "average_duration_ms": totals[4],
            "failed_replies": totals[5],
        },
        "items": [
            {
                "id": row.id,
                "request_id": row.request_id,
                "channel": row.channel,
                "started_at": utc_iso(row.started_at),
                "completed_at": utc_iso(row.completed_at),
                "duration_ms": row.duration_ms,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "tokens_complete": row.tokens_complete,
                "status": row.status,
            }
            for row in rows
        ],
    }
