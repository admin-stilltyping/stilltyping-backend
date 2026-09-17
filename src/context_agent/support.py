"""Support-ticket persistence.

The agent escalates by creating a ticket here. Tickets are idempotent on
(tenant, request_id) via a deterministic id, so a retried request never opens a
duplicate. Staff read and resolve them through the API in api.py.
"""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from modules.service import support_enabled

from .db import SupportTicket
from .schemas import DomainError


async def require_support(session, tenant):
    if not await support_enabled(session, tenant):
        raise DomainError(403, "module_disabled", "Support tickets are disabled for this business.")


def _ticket_id(tenant: str, request_id) -> UUID:
    return uuid5(NAMESPACE_URL, f"context-agent:support:{tenant}:{request_id}")


def _ref(ticket_id: UUID) -> str:
    # 12 hex chars (48 bits) so the global UniqueConstraint on ticket_ref does not
    # collide by the birthday bound at realistic ticket volumes.
    return "TKT-" + ticket_id.hex[:12].upper()


async def create_or_get(
    session,
    tenant: str,
    request_id,
    question: str,
    reason: str,
    channel: str | None = None,
    external_user_id: str | None = None,
) -> SupportTicket:
    await require_support(session, tenant)
    ticket_id = _ticket_id(tenant, str(request_id))
    row = await session.get(SupportTicket, ticket_id)
    if row is None:
        row = SupportTicket(
            id=ticket_id,
            tenant_id=tenant,
            ticket_ref=_ref(ticket_id),
            request_id=request_id,
            question=question,
            reason=reason,
            status="open",
            channel=channel,
            external_user_id=external_user_id,
        )
        session.add(row)
        await session.flush()
    return row


async def list_tickets(session, tenant: str, status: str | None = None):
    await require_support(session, tenant)
    stmt = select(SupportTicket).where(SupportTicket.tenant_id == tenant)
    if status:
        stmt = stmt.where(SupportTicket.status == status)
    stmt = stmt.order_by(SupportTicket.created_at.desc())
    return list((await session.scalars(stmt)).all())


async def get_ticket(session, tenant: str, ref: str) -> SupportTicket | None:
    await require_support(session, tenant)
    return await session.scalar(
        select(SupportTicket).where(
            SupportTicket.tenant_id == tenant, SupportTicket.ticket_ref == ref
        )
    )


async def update_ticket(
    session, tenant: str, ref: str, status: str | None = None, notes: str | None = None
) -> SupportTicket | None:
    row = await get_ticket(session, tenant, ref)
    if row is None:
        return None
    if status is not None:
        row.status = status
        row.resolved_at = datetime.now(timezone.utc) if status == "resolved" else None
    if notes is not None:
        row.notes = notes
    await session.flush()
    return row


def as_dict(ticket: SupportTicket) -> dict:
    return {
        "ticket_ref": ticket.ticket_ref,
        "tenant_id": ticket.tenant_id,
        "status": ticket.status,
        "question": ticket.question,
        "reason": ticket.reason,
        "channel": ticket.channel,
        "external_user_id": ticket.external_user_id,
        "notes": ticket.notes,
        "request_id": str(ticket.request_id),
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    }
