"""Business-owner chat using the configured agent, isolated from customer channels."""

import asyncio
from datetime import datetime
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from custom_fields.service import Owner

from .db import Conversation, Message
from .schemas import ChatInput, DomainError, KnowledgeContext

router = APIRouter(prefix="/admin/{slug}/chat", tags=["Business agent chat"])
CHANNEL = "admin_chat"


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    request_id: UUID
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class MessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    seq: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class HistoryPage(BaseModel):
    messages: list[MessageView]
    next_before: int | None


class SessionView(BaseModel):
    session_id: UUID
    title: str
    updated_at: datetime


class SessionPage(BaseModel):
    sessions: list[SessionView]
    next_offset: int | None


class Reply(BaseModel):
    messages: list[MessageView]
    knowledge_units: list[KnowledgeContext] = Field(default_factory=list)
    support_ticket: str | None = None
    response_time_ms: float | None = None


def conversation_scope(tenant, session_id=None):
    conditions = [Conversation.tenant_id == tenant, Conversation.channel == CHANNEL]
    if session_id is not None:
        conditions.append(Conversation.external_id == str(session_id))
    return conditions


@router.get("/sessions", response_model=SessionPage)
async def sessions(
    identity: Owner,
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
):
    tenant = identity.business.slug
    activity = (
        select(Message.conversation_id, func.max(Message.created_at).label("updated_at"))
        .where(Message.tenant_id == tenant)
        .group_by(Message.conversation_id)
        .subquery()
    )
    title = (
        select(Message.content)
        .where(
            Message.tenant_id == tenant,
            Message.conversation_id == Conversation.id,
            Message.role == "user",
        )
        .order_by(Message.seq)
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    async with request.app.state.services["db"].transaction() as session:
        rows = list(
            await session.execute(
                select(Conversation.external_id, title, activity.c.updated_at)
                .join(activity, activity.c.conversation_id == Conversation.id)
                .where(*conversation_scope(tenant))
                .order_by(activity.c.updated_at.desc(), Conversation.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
    return SessionPage(
        sessions=[
            SessionView(
                session_id=row[0], title=(row[1] or "Conversation")[:120], updated_at=row[2]
            )
            for row in rows[:limit]
        ],
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.get("/sessions/{session_id}/messages", response_model=HistoryPage)
async def history(
    session_id: UUID,
    identity: Owner,
    request: Request,
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    tenant = identity.business.slug
    query = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.tenant_id == tenant,
            *conversation_scope(tenant, session_id),
        )
    )
    if before is not None:
        query = query.where(Message.seq < before)
    async with request.app.state.services["db"].transaction() as session:
        rows = list(await session.scalars(query.order_by(Message.seq.desc()).limit(limit + 1)))
        visible = list(reversed(rows[:limit]))
        return HistoryPage(
            messages=[MessageView.model_validate(row) for row in visible],
            next_before=visible[0].seq if len(rows) > limit else None,
        )


async def completed_reply(db, tenant, payload, message_ids):
    async with db.transaction() as session:
        rows = list(
            await session.scalars(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.tenant_id == tenant,
                    Message.id.in_(message_ids),
                    *conversation_scope(tenant, payload.session_id),
                )
                .order_by(Message.seq)
            )
        )
        if not rows:
            return None
        if rows[0].content != payload.message:
            raise DomainError(
                409, "request_changed", "This message ID was already used for a different message."
            )
        if len(rows) != 2:
            raise DomainError(
                503,
                "chat_incomplete",
                "This reply is still being saved. Check the conversation shortly.",
            )
        return Reply(messages=[MessageView.model_validate(row) for row in rows])


@router.post("/messages", response_model=Reply)
async def send(payload: SendInput, identity: Owner, request: Request):
    services = request.app.state.services
    db, tenant = services["db"], identity.business.slug
    # Bind IDs to both the business and conversation. A retry reuses the same
    # support-ticket key and atomically persisted message pair across workers.
    request_id = uuid5(
        identity.business.id, f"admin-chat:{payload.session_id}:{payload.request_id}"
    )
    message_ids = (uuid5(request_id, "user"), uuid5(request_id, "assistant"))
    previous = await completed_reply(db, tenant, payload, message_ids)
    if previous is not None:
        return previous

    # Avoid overlapping sends from this worker without holding a DB connection
    # during the model call. Deterministic DB message IDs also protect retries.
    if not hasattr(request.app.state, "portal_chat_locks"):
        request.app.state.portal_chat_locks = {}
    locks = request.app.state.portal_chat_locks
    key = (tenant, payload.session_id)
    lock = locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise DomainError(
            409, "chat_busy", "A reply is already being prepared for this conversation."
        )
    await lock.acquire()
    started = perf_counter()
    try:
        try:
            result = await services["agent"].run(
                tenant,
                ChatInput(
                    message=payload.message,
                    request_id=request_id,
                    channel=CHANNEL,
                    external_user_id=str(payload.session_id),
                ),
                capture_enquiry=False,
                message_ids=message_ids,
            )
        except IntegrityError:
            previous = await completed_reply(db, tenant, payload, message_ids)
            if previous is not None:
                return previous
            raise
        reply = await completed_reply(db, tenant, payload, message_ids)
        if reply is None:
            raise DomainError(
                503, "chat_not_saved", "The reply could not be saved. Please retry this message."
            )
        reply.knowledge_units = [
            KnowledgeContext.model_validate(unit) for unit in result.get("knowledge_units", [])
        ]
        reply.support_ticket = next(
            (
                item["result"].get("ticket_id")
                for item in result.get("tool_results", [])
                if item["name"] == "create_support_ticket" and item["result"].get("ok")
            ),
            None,
        )
        reply.response_time_ms = round((perf_counter() - started) * 1000, 2)
        return reply
    finally:
        lock.release()
        locks.pop(key, None)
