import asyncio
import hashlib
import logging
import secrets
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from context_agent.schemas import ChatInput, DomainError
from super_admin.businesses.models import Business

from .models import VisitorSession, VisitorTurn

router = APIRouter(prefix="/web/businesses/{slug}", tags=["Public web chat"])
bearer = HTTPBearer(auto_error=False)
log = logging.getLogger(__name__)


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    message: str = Field(min_length=1, max_length=4000)


class RateLimiter:
    """Bounded, per-worker burst protection; use shared edge limits for multiple workers."""

    def __init__(self):
        self.windows = OrderedDict()

    def check(self, key, limit):
        now = monotonic()
        start, count = self.windows.pop(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        self.windows[key] = (start, count + 1)
        while len(self.windows) > 10000:
            self.windows.popitem(last=False)
        if count >= limit:
            raise DomainError(429, "chat_rate_limited", "Too many requests. Try again in a minute.")


def rate_limit(request, kind, limit):
    state = request.app.state
    if not hasattr(state, "public_chat_limiter"):
        state.public_chat_limiter = RateLimiter()
    # Do not trust arbitrary forwarded headers. Proxy trust is configured at the server.
    peer = request.client.host if request.client else "unknown"
    state.public_chat_limiter.check((kind, peer), limit)


async def active_business(session, slug):
    business = await session.scalar(
        select(Business).where(Business.slug == slug, Business.status == "active")
    )
    if business is None:
        raise DomainError(404, "chat_unavailable", "Chat is not available for this business.")
    return business


async def visitor(session, business, credentials):
    if credentials is None or len(credentials.credentials) > 200:
        raise DomainError(401, "chat_session_expired", "Start a new chat to continue.")
    row = await session.scalar(
        select(VisitorSession).where(
            VisitorSession.business_id == business.id,
            VisitorSession.token_hash
            == hashlib.sha256(credentials.credentials.encode()).hexdigest(),
            VisitorSession.expires_at > datetime.now(UTC),
        )
    )
    if row is None:
        raise DomainError(401, "chat_session_expired", "Start a new chat to continue.")
    return row


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


@router.get("/config")
async def config(slug: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.services["db"].transaction() as session:
        business = await active_business(session, slug)
        return {"slug": business.slug, "name": business.name, "max_message_length": 4000}


@router.post("/sessions", status_code=201)
async def create_session(slug: str, request: Request, response: Response):
    rate_limit(request, "sessions", 10)
    response.headers["Cache-Control"] = "no-store"
    token = secrets.token_urlsafe(32)
    async with request.app.state.services["db"].transaction(slug) as session:
        business = await active_business(session, slug)
        row = VisitorSession(
            business_id=business.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(row)
        await session.flush()
        return {"token": token, "expires_at": row.expires_at}


@router.get("/messages")
async def history(slug: str, request: Request, response: Response, credentials: Credentials):
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.services["db"].transaction() as session:
        business = await active_business(session, slug)
        owner = await visitor(session, business, credentials)
        rows = list(
            await session.scalars(
                select(VisitorTurn)
                .where(VisitorTurn.session_id == owner.id)
                .order_by(VisitorTurn.seq.desc())
                .limit(100)
            )
        )
        return {"turns": [turn_view(row) for row in reversed(rows)]}


def turn_view(row):
    return {
        "request_id": str(row.request_id),
        "message": row.message,
        "reply": row.reply,
        "status": row.status,
    }


@router.post("/messages")
async def send(
    slug: str,
    payload: MessageInput,
    request: Request,
    response: Response,
    credentials: Credentials,
):
    response.headers["Cache-Control"] = "no-store"
    services = request.app.state.services
    db = services["db"]
    async with db.transaction(slug) as session:
        business = await active_business(session, slug)
        owner = await visitor(session, business, credentials)
        turn = await session.scalar(
            select(VisitorTurn).where(
                VisitorTurn.session_id == owner.id,
                VisitorTurn.request_id == payload.request_id,
            )
        )
        if turn and turn.message != payload.message:
            raise DomainError(409, "request_changed", "This message ID was already used.")
        if turn and turn.status == "complete":
            return turn_view(turn)
        pending = await session.scalar(
            select(VisitorTurn.id).where(
                VisitorTurn.session_id == owner.id,
                VisitorTurn.status == "pending",
            )
        )
        if pending:
            raise DomainError(
                409, "chat_busy", "Your reply is still being prepared. Check again shortly."
            )
        rate_limit(request, "messages", 30)
        if turn is None:
            seq = await session.scalar(
                select(func.coalesce(func.max(VisitorTurn.seq), 0) + 1).where(
                    VisitorTurn.session_id == owner.id
                )
            )
            turn = VisitorTurn(
                session_id=owner.id, request_id=payload.request_id, message=payload.message, seq=seq
            )
            session.add(turn)
        turn.status = "pending"
        await session.flush()
        turn_id, session_id = turn.id, owner.id

    async def persist_response(session, result):
        # Commit the public reply in the same transaction as the agent's history.
        saved = await session.get(VisitorTurn, turn_id)
        saved.status, saved.reply = "complete", result["answer"]

    try:
        # Leave time for persisting a failure before the outer HTTP timeout expires.
        async with asyncio.timeout(max(0.1, services["settings"].request_timeout - 5)):
            result = await services["agent"].run(
                slug,
                ChatInput(
                    message=payload.message,
                    request_id=uuid5(session_id, str(payload.request_id)),
                    channel="web",
                    external_user_id=f"visitor:{session_id}",
                ),
                persist_response=persist_response,
            )
        async with db.transaction(slug) as session:
            turn = await session.get(VisitorTurn, turn_id)
            turn.status, turn.reply = "complete", result["answer"]
            return turn_view(turn)
    except Exception as exc:
        log.warning("Public chat failed tenant=%s error=%s", slug, type(exc).__name__)
        async with db.transaction(slug) as session:
            turn = await session.get(VisitorTurn, turn_id)
            if turn.status == "complete":
                return turn_view(turn)
            turn.status = "failed"
        raise DomainError(
            503,
            "chat_reply_unavailable",
            "A reply is unavailable right now. Please retry your message.",
        ) from None
