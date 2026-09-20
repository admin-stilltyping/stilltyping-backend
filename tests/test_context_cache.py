import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import ClientError, ServerError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import select

from context_agent import context_cache
from context_agent.config import Settings
from context_agent.context_cache import CacheRef, DaytimeContextCache, daytime_expiry
from context_agent.db import GeminiContextCache
from context_agent.models import Models

DAY = datetime(2026, 9, 20, 5, tzinfo=UTC)  # 10:30 Asia/Kolkata
END = datetime(2026, 9, 20, 16, 30, tzinfo=UTC)
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Read a fact",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


def fake_models():
    return SimpleNamespace(
        cache_credential_fingerprint="key-hash",
        create_context_cache=AsyncMock(
            return_value=SimpleNamespace(
                name="cachedContents/example",
                expire_time=END,
                usage_metadata=SimpleNamespace(total_token_count=5000),
            )
        ),
        delete_context_cache=AsyncMock(),
    )


@pytest.fixture
def now(monkeypatch):
    clock = SimpleNamespace(value=DAY)
    monkeypatch.setattr(context_cache, "utc_now", lambda: clock.value)
    return clock


def test_local_daytime_boundaries_and_midnight_end():
    assert daytime_expiry(datetime(2026, 9, 20, 4, 29, 59, tzinfo=UTC), "Asia/Kolkata") is None
    assert daytime_expiry(datetime(2026, 9, 20, 4, 30, tzinfo=UTC), "Asia/Kolkata") == END
    assert daytime_expiry(END, "Asia/Kolkata") is None
    assert daytime_expiry(END - timedelta(seconds=1), "Asia/Kolkata") == END
    assert daytime_expiry(END, "Asia/Kolkata", 10, 24).hour == 18
    with pytest.raises(ValueError):
        Settings(_env_file=None, gemini_cache_start_hour=22, gemini_cache_end_hour=10)


async def test_reuse_across_workers_tenant_isolation_and_night(db, now):
    models = fake_models()
    settings = Settings(_env_file=None)
    a = DaytimeContextCache(db, models, settings)
    b = DaytimeContextCache(db, models, settings)
    first = await a.get("dental", "rules", SCHEMAS, "Asia/Kolkata")
    assert first == CacheRef("cachedContents/example", END)
    assert await b.get("dental", "rules", SCHEMAS, "Asia/Kolkata") == first
    assert models.create_context_cache.await_count == 1
    await b.get("another-business", "rules", SCHEMAS, "Asia/Kolkata")
    assert models.create_context_cache.await_count == 2
    now.value = END
    assert await a.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    assert models.create_context_cache.await_count == 2
    now.value = DAY + timedelta(days=1)
    models.create_context_cache.return_value.expire_time = END + timedelta(days=1)
    assert await b.get("dental", "rules", SCHEMAS, "Asia/Kolkata")
    assert models.create_context_cache.await_count == 3


@pytest.mark.parametrize("change", ["instruction", "tools", "key", "model", "hours"])
async def test_changed_configuration_is_not_reused(db, now, change):
    models = fake_models()
    settings = Settings(_env_file=None)
    cache = DaytimeContextCache(db, models, settings)
    await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata")
    instruction, schemas = "rules", SCHEMAS
    if change == "instruction":
        instruction = "new rules"
    if change == "tools":
        schemas = []
    if change == "key":
        models.cache_credential_fingerprint = "new-key-hash"
    if change == "model":
        settings.chat_model = "new-model"
    if change == "hours":
        settings.gemini_cache_end_hour = 21
    await cache.get("dental", instruction, schemas, "Asia/Kolkata")
    assert models.create_context_cache.await_count == 2
    models.delete_context_cache.assert_awaited_once_with("cachedContents/example")


async def test_one_creation_for_concurrent_requests(db, now):
    models = fake_models()
    original = models.create_context_cache.return_value
    entered, finish = asyncio.Event(), asyncio.Event()

    async def create(*args):
        entered.set()
        await finish.wait()
        return original

    models.create_context_cache.side_effect = create
    cache = DaytimeContextCache(db, models, Settings(_env_file=None))
    first = asyncio.create_task(cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata"))
    await entered.wait()
    # A different worker sees the lease and proceeds with a normal request.
    second = DaytimeContextCache(db, models, Settings(_env_file=None))
    assert await second.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    finish.set()
    assert await first
    assert models.create_context_cache.await_count == 1


async def test_rejected_small_cache_is_not_retried_on_every_message(db, now, caplog):
    models = fake_models()
    models.create_context_cache.side_effect = ClientError(
        400,
        {
            "error": {"message": "Cached content is too small. PRIVATE-INSTRUCTION SECRET-KEY"},
        },
    )
    cache = DaytimeContextCache(db, models, Settings(_env_file=None))
    for _ in range(3):
        assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    assert models.create_context_cache.await_count == 1
    assert "PRIVATE-INSTRUCTION" not in caplog.text and "SECRET-KEY" not in caplog.text
    await cache.get("dental", "longer updated rules", SCHEMAS, "Asia/Kolkata")
    assert models.create_context_cache.await_count == 2


async def test_transient_cache_failure_and_expired_lease_recover(db, now):
    models = fake_models()
    created = models.create_context_cache.return_value
    models.create_context_cache.side_effect = ServerError(503, {"error": {"message": "busy"}})
    cache = DaytimeContextCache(db, models, Settings(_env_file=None))
    assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    now.value += timedelta(minutes=6)
    models.create_context_cache.side_effect = None
    assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata")
    await cache.invalidate("dental", created.name)
    async with db.transaction() as session:
        row = await session.get(GeminiContextCache, "dental")
        row.retry_after = None
        row.lease_until = now.value - timedelta(seconds=1)
    assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata")
    assert models.create_context_cache.await_count == 3


async def test_off_switch_and_closing_margin_do_not_create(db, now):
    models = fake_models()
    settings = Settings(_env_file=None, gemini_cache_enabled=False)
    cache = DaytimeContextCache(db, models, settings)
    assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    settings.gemini_cache_enabled = True
    now.value = END - timedelta(seconds=10)
    assert await cache.get("dental", "rules", SCHEMAS, "Asia/Kolkata") is None
    models.create_context_cache.assert_not_awaited()
    async with db.transaction() as session:
        assert list(await session.scalars(select(GeminiContextCache))) == []


async def test_sdk_payload_caches_only_system_and_matching_tools(monkeypatch):
    models = Models(Settings(_env_file=None, gemini_api_key="test-placeholder"))
    create = AsyncMock(return_value=SimpleNamespace(name="cachedContents/example"))
    monkeypatch.setattr(models.embeddings.aio.caches, "create", create)
    try:
        await models.create_context_cache("stable rules", SCHEMAS, END)
        config = create.call_args.kwargs["config"]
        assert config.system_instruction == "stable rules"
        assert config.contents is None
        assert config.expire_time == END and config.ttl is None
        normal = models.chat._prepare_request(
            [SystemMessage("stable rules"), HumanMessage("question")],
            tools=SCHEMAS,
        )
        assert config.tools == normal["config"].tools
        cached = models.chat._prepare_request(
            [HumanMessage("fresh KB and clock"), HumanMessage("question")],
            cached_content="cachedContents/example",
        )
        assert cached["config"].system_instruction is None
        assert cached["config"].tools is None
        assert cached["config"].cached_content == "cachedContents/example"
        # Follow-up rounds retain names/arguments/results without rebinding tools.
        followup = models.chat._prepare_request(
            [
                HumanMessage("fresh context"),
                HumanMessage("question"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {"query": "fee"}, "id": "call"}],
                ),
                ToolMessage(content='{"ok": true}', tool_call_id="call"),
            ],
            cached_content="cachedContents/example",
        )
        assert followup["contents"][-1].parts[0].function_response.name == "lookup"
    finally:
        await models.close()


async def test_agent_cached_request_keeps_runtime_context_and_customer_history_fresh(db, now):
    from uuid import uuid4

    from test_agent import KnowledgeRetriever

    from context_agent.agent import Agent
    from context_agent.schemas import ChatInput

    models = fake_models()
    models.query = AsyncMock(return_value=[1.0, 0.0, 0.0])
    models.chat = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="INR 500")))
    agent = Agent(db, models, KnowledgeRetriever(), Settings(_env_file=None))
    request = ChatInput(message="new question", request_id=uuid4())
    state = await agent.retrieve(
        {
            "tenant": "dental",
            "request": request,
            "history": [{"role": "user", "content": "private history"}],
        }
    )
    sent_instruction = models.create_context_cache.call_args.args[0]
    assert "Consultation costs" not in sent_instruction
    assert "private history" not in sent_instruction and "new question" not in sent_instruction
    assert "CURRENT BUSINESS TIME" not in sent_instruction
    await agent.reason({**state, "tenant": "dental"})
    messages = models.chat.ainvoke.call_args.args[0]
    assert not any(isinstance(message, SystemMessage) for message in messages)
    assert "KNOWLEDGE:" in messages[0].content
    assert messages[-2].content == "private history" and messages[-1].content == "new question"
    assert models.chat.ainvoke.call_args.kwargs == {"cached_content": "cachedContents/example"}


@pytest.mark.parametrize(
    "failure, fallback",
    [
        (ClientError(404, {"error": {"message": "not found"}}), True),
        (ClientError(400, {"error": {"message": "cached content invalid"}}), True),
        (ClientError(429, {"error": {"message": "quota"}}), False),
        (ServerError(503, {"error": {"message": "unavailable"}}), False),
        (TimeoutError(), False),
    ],
)
async def test_agent_rejected_cache_falls_back_once_but_ambiguous_failures_do_not(
    db, now, failure, fallback
):
    from unittest.mock import Mock

    from context_agent.agent import Agent
    from context_agent.schemas import DomainError

    models = fake_models()
    normal = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="answer")))
    models.chat = SimpleNamespace(
        ainvoke=AsyncMock(side_effect=failure),
        bind_tools=Mock(return_value=normal),
    )
    agent = Agent(db, models, None, Settings(_env_file=None))
    agent.context_cache.invalidate = AsyncMock()
    state = {
        "tenant": "dental",
        "cache_ref": CacheRef("cachedContents/example", END),
        "schemas": SCHEMAS,
        "dynamic_context": "fresh KB",
        "messages": [SystemMessage("full business rules and fresh KB"), HumanMessage("question")],
    }
    if fallback:
        result = await agent.reason(state)
        assert result["cache_ref"] is None
        assert result["messages"][-1].content == "answer"
        normal.ainvoke.assert_awaited_once_with(state["messages"])
        models.chat.bind_tools.assert_called_once_with(SCHEMAS)
        agent.context_cache.invalidate.assert_awaited_once()
    else:
        with pytest.raises(DomainError):
            await agent.reason(state)
        normal.ainvoke.assert_not_awaited()
        agent.context_cache.invalidate.assert_not_awaited()


async def test_tool_round_crossing_expiry_uses_full_instructions_without_new_cache(db, now):
    from unittest.mock import Mock

    from context_agent.agent import Agent

    models = fake_models()
    normal = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="answer")))
    models.chat = SimpleNamespace(ainvoke=AsyncMock(), bind_tools=Mock(return_value=normal))
    agent = Agent(db, models, None, Settings(_env_file=None))
    state = {
        "tenant": "dental",
        "cache_ref": CacheRef("cachedContents/example", END),
        "schemas": SCHEMAS,
        "dynamic_context": "fresh KB",
        "messages": [SystemMessage("full business rules and fresh KB"), HumanMessage("question")],
    }
    now.value = END
    result = await agent.reason(state)
    assert result["cache_ref"] is None
    normal.ainvoke.assert_awaited_once_with(state["messages"])
    models.chat.ainvoke.assert_not_awaited()
    models.create_context_cache.assert_not_awaited()
