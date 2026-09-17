import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.agent import Agent
from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import Conversation, Message
from crm.models import Customer, Enquiry, Lead
from modules.models import BusinessModules
from public_chat.models import VisitorSession, VisitorTurn
from public_chat.routes import RateLimiter
from super_admin.businesses.models import Business
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business


@pytest.fixture
async def chat(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="public-chat-test-secret-long-enough"
    )
    businesses = []
    for name in ("Web Chat A", "Web Chat B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            modules = await session.get(BusinessModules, business.id)
            modules.leads = True
        businesses.append(business)
    control = SimpleNamespace(fail=False, calls=[], started=asyncio.Event(), release=None)

    async def reply(state, config):
        control.calls.append(state)
        control.started.set()
        if control.release:
            await control.release.wait()
        if control.fail:
            raise RuntimeError("private-provider-key-never-expose")
        return {
            "answer": "Hello from " + state["tenant"],
            "outcome": "answered",
            "results": [{"internal": "private-tool-result"}],
            "knowledge": [{"private": "source"}],
        }

    agent = Agent(db, None, None, settings)
    agent.graph = SimpleNamespace(ainvoke=reply)
    services = {"db": db, "settings": settings, "agent": agent}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            db=db, client=client, app=app, a=businesses[0], b=businesses[1], control=control
        )


async def visit(c, business=None):
    business = business or c.a
    response = await c.client.post(f"/web/businesses/{business.slug}/sessions")
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return {"Authorization": "Bearer " + response.json()["token"]}


async def send(c, headers, body=None, business=None):
    return await c.client.post(
        f"/web/businesses/{(business or c.a).slug}/messages",
        headers=headers,
        json=body or {"request_id": str(uuid4()), "message": "What are your opening hours?"},
    )


async def history(c, headers=None, business=None):
    return await c.client.get(f"/web/businesses/{(business or c.a).slug}/messages", headers=headers)


async def count(c, model):
    async with c.db.transaction() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_public_config_returns_saved_name_and_only_public_fields(chat):
    c = chat
    response = await c.client.get(f"/web/businesses/{c.a.slug}/config")
    assert response.json() == {"slug": c.a.slug, "name": c.a.name, "max_message_length": 4000}
    assert response.headers["cache-control"] == "no-store"
    assert (await c.client.get("/web/businesses/missing/config")).status_code == 404


async def test_visitor_tokens_are_hashed_and_history_is_private(chat):
    c = chat
    first, second = await visit(c), await visit(c)
    await send(c, first)
    assert len((await history(c, first)).json()["turns"]) == 1
    assert (await history(c, second)).json() == {"turns": []}
    assert (await history(c)).status_code == 401
    assert (await history(c, {"Authorization": "Bearer guessed"})).status_code == 401
    assert (await history(c, first, c.b)).status_code == 401
    assert (await send(c, first, business=c.b)).status_code == 401
    async with c.db.transaction() as session:
        tokens = list(await session.scalars(select(VisitorSession.token_hash)))
    raw = first["Authorization"].split()[1]
    assert raw not in tokens and hashlib.sha256(raw.encode()).hexdigest() in tokens


async def test_web_enquiries_group_without_creating_a_customer(chat):
    c = chat
    headers = await visit(c)
    for message in ("First enquiry", "Second enquiry"):
        response = await send(c, headers, {"request_id": str(uuid4()), "message": message})
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"request_id", "message", "reply", "status"}
        assert "private" not in response.text
    assert await count(c, Lead) == 1
    assert await count(c, Enquiry) == 2
    assert await count(c, Customer) == 0
    assert await count(c, Conversation) == 1
    assert await count(c, Message) == 4
    assert [x["message"] for x in (await history(c, headers)).json()["turns"]] == [
        "First enquiry",
        "Second enquiry",
    ]
    assert c.control.calls[1]["history"][0]["content"] == "First enquiry"
    assert c.control.calls[0]["request"].channel == "web"
    assert c.control.calls[0]["request"].external_user_id.startswith("visitor:")


async def test_retries_return_same_reply_without_duplicate_agent_or_enquiry(chat):
    c = chat
    headers = await visit(c)
    body = {"request_id": str(uuid4()), "message": "Hello"}
    first = await send(c, headers, body)
    assert (await send(c, headers, body)).json() == first.json()
    assert (await send(c, headers, {**body, "message": "Different"})).status_code == 409
    assert len(c.control.calls) == 1
    assert await count(c, Enquiry) == 1 and await count(c, Message) == 2
    # Request IDs are scoped to the visitor, not a public global namespace.
    assert (await send(c, await visit(c), body)).status_code == 200
    assert await count(c, Lead) == 2


async def test_failed_ai_keeps_enquiry_and_retry_does_not_duplicate_it(chat):
    c = chat
    headers = await visit(c)
    body = {"request_id": str(uuid4()), "message": "Hello"}
    c.control.fail = True
    response = await send(c, headers, body)
    assert response.status_code == 503 and "private" not in response.text
    assert (await history(c, headers)).json()["turns"][0]["status"] == "failed"
    assert await count(c, Enquiry) == 1 and await count(c, Message) == 0
    c.control.fail = False
    assert (await send(c, headers, body)).json()["status"] == "complete"
    assert await count(c, Enquiry) == 1 and await count(c, Message) == 2


async def test_simultaneous_sends_are_serialized_per_visitor(chat):
    c = chat
    c.control.release = asyncio.Event()
    headers = await visit(c)
    first = asyncio.create_task(send(c, headers))
    try:
        await asyncio.wait_for(c.control.started.wait(), timeout=3)
        assert (await send(c, headers)).status_code == 409
        assert (await history(c, headers)).json()["turns"][0]["status"] == "pending"
    finally:
        c.control.release.set()
    assert (await first).status_code == 200
    assert len(c.control.calls) == 1


async def test_leads_disabled_still_allows_chat_without_creating_leads(chat):
    c = chat
    async with c.db.transaction() as session:
        modules = await session.get(BusinessModules, c.a.id)
        modules.leads = False
    assert (await send(c, await visit(c))).status_code == 200
    assert await count(c, Lead) == 0 and await count(c, Customer) == 0


async def test_expired_session_and_inactive_business_are_rejected(chat):
    c = chat
    headers = await visit(c)
    async with c.db.transaction() as session:
        row = await session.scalar(select(VisitorSession))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await history(c, headers)).status_code == 401
    assert (await send(c, headers)).status_code == 401
    async with c.db.transaction() as session:
        row = await session.get(Business, c.a.id)
        row.status = "suspended"
    assert (await c.client.post(f"/web/businesses/{c.a.slug}/sessions")).status_code == 404
    assert (await send(c, headers)).status_code == 404
    assert (await c.client.get(f"/web/businesses/{c.a.slug}/config")).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"message": " "},
        {"message": "x" * 4001},
        {"request_id": "bad"},
        {"external_user_id": "victim"},
        {"business_slug": "victim"},
        {"channel": "instagram"},
        {"admin_mode": True},
        {"model": "unauthorized-model"},
    ],
)
async def test_public_payload_rejects_invalid_or_privileged_fields(chat, changes):
    c = chat
    body = {"request_id": str(uuid4()), "message": "Hello", **changes}
    assert (await send(c, await visit(c), body)).status_code == 400
    assert await count(c, VisitorTurn) == 0


async def test_session_creation_rate_limit(chat):
    c = chat
    for _ in range(10):
        await visit(c)
    response = await c.client.post(f"/web/businesses/{c.a.slug}/sessions")
    assert response.status_code == 429


def test_burst_limits_reset_and_keep_bounded_memory(monkeypatch):
    limiter = RateLimiter()
    now = [1.0]
    monkeypatch.setattr("public_chat.routes.monotonic", lambda: now[0])
    limiter.check("visitor", 1)
    from context_agent.schemas import DomainError

    with pytest.raises(DomainError):
        limiter.check("visitor", 1)
    now[0] += 61
    limiter.check("visitor", 1)
    for i in range(10001):
        limiter.check(str(i), 1)
    assert len(limiter.windows) == 10000
