"""Cross-tenant request timing for super-admins diagnosing slow replies.

Surfaces the same phase breakdown as the business-owner AI usage endpoint
(see ai_usage.py), but across every tenant, to locate where a slow webhook
reply spent its time: queueing before processing, database time, model/tool
time, and the channel send. No message content, tokens cost, or credentials
are exposed here.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select

from super_admin.businesses.models import Business
from super_admin.routes import require_super_admin

from .ai_usage import utc_iso
from .db import AiUsageRecord as Usage
from .schemas import DomainError

router = APIRouter(
    prefix="/super-admin/request-timing",
    tags=["Super-admin request timing"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("")
async def list_request_timing(
    request: Request,
    business: Annotated[str | None, Query(min_length=1, max_length=63)] = None,
    channel: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    status: Literal["completed", "failed", "awaiting_send", "send_failed"] | None = None,
    start: date | None = None,
    end: date | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
):
    today = datetime.now(UTC).date()
    start, end = start or today - timedelta(days=1), end or today
    if end < start or end == date.max:
        raise DomainError(
            400, "invalid_date_range", "Choose an end date on or after the start date."
        )
    conditions = [
        Usage.started_at >= datetime.combine(start, time.min, UTC),
        Usage.started_at < datetime.combine(end + timedelta(days=1), time.min, UTC),
    ]
    if business:
        conditions.append(Usage.tenant_id == business)
    if channel:
        conditions.append(Usage.channel == channel)
    if status:
        conditions.append(Usage.status == status)
    async with request.app.state.services["db"].transaction() as session:
        totals = (
            await session.execute(
                select(
                    func.count(Usage.id),
                    func.avg(Usage.duration_ms),
                    func.max(Usage.duration_ms),
                    func.avg(Usage.queue_ms),
                    func.avg(Usage.db_ms),
                    func.avg(Usage.ai_ms),
                    func.avg(Usage.send_ms),
                    func.count(case((Usage.status.in_(["failed", "send_failed"]), 1))),
                ).where(*conditions)
            )
        ).one()
        rows = list(
            await session.execute(
                select(Usage, Business.name, Business.slug)
                .outerjoin(Business, Business.slug == Usage.tenant_id)
                .where(*conditions)
                .order_by(Usage.started_at.desc(), Usage.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
    return {
        "start": start,
        "end": end,
        "total_count": totals[0],
        "next_offset": offset + limit if offset + limit < totals[0] else None,
        "summary": {
            "replies": totals[0],
            "average_duration_ms": totals[1],
            "slowest_duration_ms": totals[2],
            "average_queue_ms": totals[3],
            "average_db_ms": totals[4],
            "average_ai_ms": totals[5],
            "average_send_ms": totals[6],
            "failed_replies": totals[7],
        },
        "items": [
            {
                "id": row.id,
                "request_id": row.request_id,
                "business_slug": row.tenant_id,
                "business_name": name or row.tenant_id,
                "channel": row.channel,
                "started_at": utc_iso(row.started_at),
                "completed_at": utc_iso(row.completed_at),
                "status": row.status,
                "duration_ms": row.duration_ms,
                "queue_ms": row.queue_ms,
                "db_ms": row.db_ms,
                "ai_ms": row.ai_ms,
                "tool_ms": row.tool_ms,
                "send_ms": row.send_ms,
            }
            for row, name, _slug in rows
        ],
    }
