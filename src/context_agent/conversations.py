"""Conversation persistence: store the input and output messages of a chat and
replay recent turns back to the agent.

A conversation is keyed by (tenant_id, channel, external_id) — the same customer
on the same channel always resumes one thread. Messages are ordered by a
per-conversation ``seq``, computed as MAX(seq)+1 under the tenant advisory lock
that ``Database.transaction(tenant)`` already holds, so concurrent writers for a
tenant are serialized and cannot collide on the same sequence number.
"""

from uuid import UUID, uuid4

from sqlalchemy import func, select

from .db import Conversation, Message


async def get_or_create(session, tenant: str, channel: str, external_id: str) -> UUID:
    row = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant,
            Conversation.channel == channel,
            Conversation.external_id == external_id,
        )
    )
    if row is None:
        row = Conversation(id=uuid4(), tenant_id=tenant, channel=channel, external_id=external_id)
        session.add(row)
        await session.flush()
    return row.id


async def load_history(session, tenant: str, conversation_id: UUID, limit: int) -> list[dict]:
    """The most recent ``limit`` messages, oldest-first, as {role, content} dicts."""
    if limit <= 0:
        return []
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.tenant_id == tenant,
                    Message.conversation_id == conversation_id,
                )
                .order_by(Message.seq.desc())
                .limit(limit)
            )
        ).all()
    )
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


async def record_message(
    session,
    tenant: str,
    conversation_id: UUID,
    role: str,
    content: str,
    *,
    message_id: UUID | None = None,
) -> None:
    next_seq = await session.scalar(
        select(func.coalesce(func.max(Message.seq), 0) + 1).where(
            Message.conversation_id == conversation_id
        )
    )
    session.add(
        Message(
            id=message_id or uuid4(),
            tenant_id=tenant,
            conversation_id=conversation_id,
            seq=next_seq,
            role=role,
            content=content,
        )
    )
    await session.flush()
