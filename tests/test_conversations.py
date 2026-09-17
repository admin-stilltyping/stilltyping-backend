from collections import deque
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FakeModels
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import func, select

from context_agent import conversations
from context_agent.agent import Agent
from context_agent.db import Message
from context_agent.schemas import ChatInput, DomainError


class Chat:
    def __init__(self, responses):
        self.responses = deque(responses)

    def bind_tools(self, schemas):
        return self

    async def ainvoke(self, messages):
        self.last_messages = messages
        return self.responses.popleft()


class KnowledgeRetriever:
    async def search(self, *args, kind="knowledge_units", **kwargs):
        return (
            []
            if kind == "tools"
            else [SimpleNamespace(id=uuid4(), title="Fee", content="Consultation costs INR 500.")]
        )


def settings():
    return SimpleNamespace(max_tool_rounds=2, support_webhook_url=None, history_limit=10)


async def message_count(db, conversation_id):
    async with db.transaction() as session:
        return await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == conversation_id
            )
        )


# -- store ---------------------------------------------------------------


async def test_get_or_create_is_idempotent_and_tenant_scoped(db):
    async with db.transaction("t1") as s:
        first = await conversations.get_or_create(s, "t1", "web", "user-a")
        again = await conversations.get_or_create(s, "t1", "web", "user-a")
        other_channel = await conversations.get_or_create(s, "t1", "whatsapp", "user-a")
    async with db.transaction("t2") as s:
        other_tenant = await conversations.get_or_create(s, "t2", "web", "user-a")
    assert first == again
    assert len({first, other_channel, other_tenant}) == 3


async def test_history_is_ordered_and_limited(db):
    async with db.transaction("t") as s:
        conv = await conversations.get_or_create(s, "t", "web", "u")
        for i in range(6):
            await conversations.record_message(s, "t", conv, "user", f"q{i}")
            await conversations.record_message(s, "t", conv, "assistant", f"a{i}")
    async with db.transaction("t") as s:
        recent = await conversations.load_history(s, "t", conv, limit=3)
        full = await conversations.load_history(s, "t", conv, limit=100)
    # Newest window, returned oldest-first.
    assert [m["content"] for m in recent] == ["a4", "q5", "a5"]
    assert full[0]["content"] == "q0" and full[-1]["content"] == "a5"
    assert len(full) == 12


async def test_zero_limit_returns_no_history(db):
    async with db.transaction("t") as s:
        conv = await conversations.get_or_create(s, "t", "web", "u")
        await conversations.record_message(s, "t", conv, "user", "hi")
        assert await conversations.load_history(s, "t", conv, limit=0) == []


# -- agent integration ---------------------------------------------------


async def test_stateless_request_stores_nothing(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="Consultation costs INR 500.")])
    result = await Agent(db, models, KnowledgeRetriever(), settings()).run(
        "t", ChatInput(message="Fee?", request_id=uuid4())
    )
    assert result["conversation_id"] is None
    async with db.transaction() as s:
        total = await s.scalar(select(func.count()).select_from(Message))
    assert total == 0


async def test_conversation_stores_input_output_and_replays_history(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="Hi there."), AIMessage(content="Goodbye.")])
    agent = Agent(db, models, KnowledgeRetriever(), settings())

    first = await agent.run(
        "t", ChatInput(message="Hello", request_id=uuid4(), external_user_id="u1")
    )
    assert first["conversation_id"] is not None
    assert first["answer"] == "Hi there."

    second = await agent.run(
        "t", ChatInput(message="Second", request_id=uuid4(), external_user_id="u1")
    )
    assert second["conversation_id"] == first["conversation_id"]

    # The second turn's prompt: system, then the first exchange as history, then the new message.
    sent = models.chat.last_messages
    assert isinstance(sent[0], SystemMessage)
    assert isinstance(sent[1], HumanMessage) and sent[1].content == "Hello"
    assert isinstance(sent[2], AIMessage) and sent[2].content == "Hi there."
    assert isinstance(sent[-1], HumanMessage) and sent[-1].content == "Second"

    async with db.transaction("t") as s:
        stored = await conversations.load_history(s, "t", first["conversation_id"], 100)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", "Hello"),
        ("assistant", "Hi there."),
        ("user", "Second"),
        ("assistant", "Goodbye."),
    ]


async def test_failed_turn_records_no_orphan_message(db):
    class BoomChat:
        def bind_tools(self, schemas):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("model down")

    models = FakeModels()
    models.chat = BoomChat()
    agent = Agent(db, models, KnowledgeRetriever(), settings())
    with pytest.raises(DomainError):
        await agent.run(
            "t", ChatInput(message="Hello", request_id=uuid4(), external_user_id="u1")
        )
    # The turn failed, so nothing is persisted (no orphan user message to pollute history).
    async with db.transaction() as session:
        total = await session.scalar(select(func.count()).select_from(Message))
    assert total == 0


async def test_different_users_do_not_share_history(db):
    models = FakeModels()
    models.chat = Chat([AIMessage(content="A1"), AIMessage(content="B1")])
    agent = Agent(db, models, KnowledgeRetriever(), settings())
    a = await agent.run("t", ChatInput(message="from A", request_id=uuid4(), external_user_id="A"))
    await agent.run("t", ChatInput(message="from B", request_id=uuid4(), external_user_id="B"))
    # B's turn must not carry A's message.
    assert all(getattr(m, "content", None) != "from A" for m in models.chat.last_messages)
    assert await message_count(db, a["conversation_id"]) == 2
