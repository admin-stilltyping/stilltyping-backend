from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FakeModels
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from context_agent import conversations, instructions
from context_agent.agent import Agent
from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import Message, SupportTicket
from crm.models import Enquiry, Lead
from modules.models import BusinessModules
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business


class Model:
    def __init__(self):
        self.calls = []
        self.fail = False

    def bind_tools(self, schemas):
        self.schemas = schemas
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("test model unavailable")
        return AIMessage(content="Consultations cost INR 500.\nPlease call for availability.")


class Retriever:
    enabled = True

    async def search(self, *args, kind="knowledge_units", **kwargs):
        return (
            [
                SimpleNamespace(
                    id=uuid4(), title="Consultations", content="Consultations cost INR 500."
                )
            ]
            if kind == "knowledge_units" and self.enabled
            else []
        )


@pytest.fixture
async def chat(db):
    settings = Settings(
        _env_file=None,
        tenant_api_key="server-only-test-key",
        super_admin_jwt_secret="portal-chat-tests-signing-secret-32-bytes",
    )
    owners = []
    for name in ("Chat Clinic A", "Chat Clinic B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            modules = await session.get(BusinessModules, business.id)
            modules.leads = True
            admin = await session.scalar(
                select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
            )
        owners.append(
            SimpleNamespace(
                business=business,
                headers={
                    "Authorization": "Bearer "
                    + create_business_token(BusinessIdentity(admin, business), settings)
                },
            )
        )
    models = FakeModels()
    models.chat = Model()
    retriever = Retriever()
    services = {"db": db, "settings": settings, "agent": Agent(db, models, retriever, settings)}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            db=db,
            a=owners[0],
            b=owners[1],
            client=client,
            model=models.chat,
            retriever=retriever,
            app=app,
        )


def payload(message="What is the consultation fee?", session_id=None, request_id=None):
    return {
        "session_id": str(session_id or uuid4()),
        "request_id": str(request_id or uuid4()),
        "message": message,
    }


def base(owner):
    return f"/admin/{owner.business.slug}/chat"


async def send(c, data, owner=None):
    owner = owner or c.a
    return await c.client.post(base(owner) + "/messages", headers=owner.headers, json=data)


async def test_owner_auth_and_internal_key_boundary(chat):
    c = chat
    for method, path in [
        ("GET", "/sessions"),
        ("GET", f"/sessions/{uuid4()}/messages"),
        ("POST", "/messages"),
    ]:
        for headers in ({}, {"X-Tenant-API-Key": "server-only-test-key"}):
            r = await c.client.request(
                method,
                base(c.a) + path,
                headers=headers,
                json=payload() if method == "POST" else None,
            )
            assert r.status_code == 401
        r = await c.client.request(
            method,
            base(c.a) + path,
            headers=c.b.headers,
            json=payload() if method == "POST" else None,
        )
        assert r.status_code == 403
    assert not c.model.calls
    r = await c.client.post(
        f"/api/v1/tenants/{c.a.business.slug}/agent/messages",
        headers=c.a.headers,
        json={"message": "Hi", "request_id": str(uuid4())},
    )
    assert r.status_code == 401


async def test_default_agent_history_instructions_and_no_admin_leads(chat):
    c = chat
    data = payload()
    async with c.db.transaction() as session:
        await instructions.set_instructions(session, c.a.business.slug, "Keep the answer friendly.")
    first = await send(c, data)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    result = first.json()
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert result["knowledge_units"][0]["title"] == "Consultations"
    assert "Keep the answer friendly." in c.model.calls[0][0].content
    second = await send(c, payload("And how much was that?", session_id=data["session_id"]))
    assert second.status_code == 200
    assert c.model.calls[1][1].content == data["message"]
    sessions = await c.client.get(base(c.a) + "/sessions", headers=c.a.headers)
    assert sessions.json()["sessions"][0]["session_id"] == data["session_id"]
    assert sessions.json()["sessions"][0]["title"] == data["message"]
    history = await c.client.get(
        base(c.a) + f"/sessions/{data['session_id']}/messages", headers=c.a.headers
    )
    assert [m["seq"] for m in history.json()["messages"]] == [1, 2, 3, 4]
    async with c.db.transaction() as session:
        for model in (Lead, Enquiry):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_retry_reuses_saved_reply_without_duplicate_turn_or_model_call(chat):
    c = chat
    data = payload()
    first = await send(c, data)
    retry = await send(c, data)
    assert first.status_code == retry.status_code == 200
    assert first.json()["messages"] == retry.json()["messages"]
    assert len(c.model.calls) == 1
    changed = await send(c, {**data, "message": "Different text"})
    assert changed.status_code == 409
    async with c.db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 2


async def test_customer_channels_and_other_businesses_are_isolated(chat):
    c = chat
    data = payload()
    await send(c, data)
    async with c.db.transaction() as session:
        conv = await conversations.get_or_create(
            session, c.a.business.slug, "instagram", data["session_id"]
        )
        await conversations.record_message(
            session, c.a.business.slug, conv, "user", "Private customer message"
        )
    for owner in (c.a, c.b):
        listing = await c.client.get(base(owner) + "/sessions", headers=owner.headers)
        history = await c.client.get(
            base(owner) + f"/sessions/{data['session_id']}/messages", headers=owner.headers
        )
        assert "Private customer message" not in listing.text + history.text
        if owner == c.b:
            assert listing.json()["sessions"] == [] and history.json()["messages"] == []
    await send(c, data, owner=c.b)
    assert len(c.model.calls) == 2
    assert len(c.model.calls[1]) == 2  # system and current user; no other business history


async def test_history_and_session_pagination(chat):
    c = chat
    first = payload()
    await send(c, first)
    await send(c, payload("A follow-up", session_id=first["session_id"]))
    await send(c, payload("Separate session"))
    listing = await c.client.get(base(c.a) + "/sessions?limit=1", headers=c.a.headers)
    assert len(listing.json()["sessions"]) == 1 and listing.json()["next_offset"] == 1
    next_page = await c.client.get(base(c.a) + "/sessions?limit=1&offset=1", headers=c.a.headers)
    assert (
        listing.json()["sessions"][0]["session_id"] != next_page.json()["sessions"][0]["session_id"]
    )
    url = base(c.a) + f"/sessions/{first['session_id']}/messages"
    recent = await c.client.get(url + "?limit=2", headers=c.a.headers)
    assert [m["seq"] for m in recent.json()["messages"]] == [3, 4]
    older = await c.client.get(
        url + f"?limit=2&before={recent.json()['next_before']}", headers=c.a.headers
    )
    assert [m["seq"] for m in older.json()["messages"]] == [1, 2]
    assert older.json()["next_before"] is None


async def test_model_failure_can_retry_without_an_orphan_user_message(chat):
    c = chat
    data = payload()
    c.model.fail = True
    assert (await send(c, data)).status_code == 502
    listing = await c.client.get(base(c.a) + "/sessions", headers=c.a.headers)
    assert listing.json()["sessions"] == []
    c.model.fail = False
    assert (await send(c, data)).status_code == 200
    async with c.db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 2


async def test_support_escalation_creates_one_real_ticket_on_retry(chat):
    c = chat
    c.retriever.enabled = False
    data = payload("Unknown policy")
    response = await send(c, data)
    assert response.status_code == 200, response.text
    assert response.json()["support_ticket"].startswith("TKT-")
    assert (await send(c, data)).status_code == 200
    async with c.db.transaction() as session:
        rows = list(await session.scalars(select(SupportTicket)))
        assert len(rows) == 1 and rows[0].channel == "admin_chat"
        assert await session.scalar(select(func.count()).select_from(Lead)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"model": "different-model"},
        {"provider": "other"},
        {"business_slug": "other"},
        {"channel": "web"},
        {"message": " "},
        {"message": "x" * 4001},
        {"session_id": "invalid"},
    ],
)
async def test_invalid_fields_and_model_overrides_are_rejected(chat, change):
    c = chat
    assert (await send(c, {**payload(), **change})).status_code == 400
    assert c.model.calls == []


async def test_suspended_business_cannot_chat(chat):
    c = chat
    async with c.db.transaction() as session:
        business = await session.get(Business, c.a.business.id)
        business.status = "suspended"
    assert (await send(c, payload())).status_code == 401
    assert c.model.calls == []
