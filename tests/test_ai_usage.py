import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import test_portal_chat
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from test_portal_chat import base, payload, send
from test_usage import result as llm_result
from test_webhooks import wa_payload

from context_agent.channels import ADAPTERS
from context_agent.db import AiUsageRecord
from context_agent.schemas import ChatInput
from context_agent.usage import TokenUsage, UsageCallback, current_usage
from context_agent.webhooks import process_webhook

chat = test_portal_chat.chat


def record(tenant, when, channel="instagram", **kwargs):
    return AiUsageRecord(
        id=uuid4(),
        tenant_id=tenant,
        request_id=uuid4(),
        channel=channel,
        started_at=when,
        completed_at=when + timedelta(seconds=2),
        duration_ms=2000,
        input_tokens=kwargs.pop("input_tokens", 100),
        output_tokens=20,
        tokens_complete=kwargs.pop("tokens_complete", True),
        llm_calls=kwargs.pop("llm_calls", 1),
        status=kwargs.pop("status", "completed"),
        **kwargs,
    )


def endpoint(owner):
    return f"/admin/{owner.business.slug}/ai-usage"


async def usage_rows(c):
    async with c.db.transaction() as session:
        return list(await session.scalars(select(AiUsageRecord).order_by(AiUsageRecord.started_at)))


async def test_owner_auth_timezone_filters_summary_and_pagination(chat):
    c = chat
    async with c.db.transaction() as session:
        # 18:30 UTC is midnight in the business's Asia/Kolkata timezone.
        session.add_all(
            [
                record(c.a.business.slug, datetime(2026, 9, 17, 18, 29, 59, tzinfo=UTC)),
                record(c.a.business.slug, datetime(2026, 9, 17, 18, 30, tzinfo=UTC)),
                record(
                    c.a.business.slug,
                    datetime(2026, 9, 18, 12, tzinfo=UTC),
                    "admin_chat",
                    tokens_complete=False,
                    input_tokens=10,
                    status="failed",
                ),
                record(c.a.business.slug, datetime(2026, 9, 18, 18, 30, tzinfo=UTC)),
                record(c.b.business.slug, datetime(2026, 9, 18, 12, tzinfo=UTC), input_tokens=999),
            ]
        )
    url = endpoint(c.a)
    assert (await c.client.get(url)).status_code == 401
    assert (await c.client.get(url, headers=c.b.headers)).status_code == 403
    params = {"start": "2026-09-18", "end": "2026-09-18", "limit": 1}
    response = await c.client.get(url, params=params, headers=c.a.headers)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    data = response.json()
    assert data["timezone"] == "Asia/Kolkata"
    assert data["total_count"] == 2 and data["next_offset"] == 1
    assert data["items"][0]["channel"] == "admin_chat"
    assert data["items"][0]["started_at"].endswith("+00:00")
    assert data["summary"] == {
        "replies": 2,
        "input_tokens": 110,
        "output_tokens": 40,
        "incomplete_replies": 1,
        "failed_replies": 1,
        "average_duration_ms": 2000,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "cache_incomplete_replies": 2,
        "cached_replies": 0,
        "uncached_replies": 0,
    }
    second = (await c.client.get(url, params={**params, "offset": 1}, headers=c.a.headers)).json()
    assert second["next_offset"] is None
    assert second["items"][0]["id"] != data["items"][0]["id"]
    filtered = (
        await c.client.get(url, params={**params, "channel": "instagram"}, headers=c.a.headers)
    ).json()
    assert filtered["summary"]["replies"] == 1
    assert filtered["summary"]["input_tokens"] == 100
    invalid = await c.client.get(
        url, params={"start": "2026-09-19", "end": "2026-09-18"}, headers=c.a.headers
    )
    assert invalid.status_code == 400


async def test_empty_default_range_and_unknown_values(chat):
    response = await chat.client.get(endpoint(chat.a), headers=chat.a.headers)
    data = response.json()
    assert data["start"].endswith("-01")
    assert data["items"] == [] and data["next_offset"] is None
    assert data["summary"]["average_duration_ms"] is None
    assert data["summary"]["input_tokens"] == 0
    assert data["summary"]["cached_replies"] == 0
    assert data["summary"]["uncached_replies"] == 0
    assert data["summary"]["cache_incomplete_replies"] == 0


async def test_agent_chat_records_all_calls_once_and_cached_retry_does_not_add_usage(
    chat, monkeypatch
):
    c = chat
    original = c.model.ainvoke

    async def counted(messages):
        callback = UsageCallback()
        run = uuid4()
        callback.on_llm_end(llm_result(100, 20), run_id=run)
        callback.on_llm_end(llm_result(100, 20), run_id=run)
        callback.on_llm_end(llm_result(30, 5), run_id=uuid4())
        return await original(messages)

    monkeypatch.setattr(c.model, "ainvoke", counted)
    request = payload()
    first = await send(c, request)
    assert first.status_code == 200
    assert (await send(c, request)).status_code == 200
    assert (await c.client.get(base(c.a) + "/sessions", headers=c.a.headers)).status_code == 200
    rows = await usage_rows(c)
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "admin_chat" and row.tenant_id == c.a.business.slug
    assert row.input_tokens == 130 and row.output_tokens == 25
    assert row.tokens_complete and row.llm_calls == 2
    assert row.status == "completed" and row.duration_ms > 0
    assert row.conversation_id is not None
    assert current_usage.get() is None


async def test_failed_attempt_then_retry_records_both_and_preserves_parent_usage(chat, monkeypatch):
    c = chat
    original = c.model.ainvoke

    async def counted(messages):
        UsageCallback().on_llm_end(llm_result(12, 3), run_id=uuid4())
        if c.model.fail:
            UsageCallback().on_llm_error(RuntimeError(), run_id=uuid4())
        return await original(messages)

    monkeypatch.setattr(c.model, "ainvoke", counted)
    agent = c.app.state.services["agent"]
    request = ChatInput(message="price?", channel="admin_chat", request_id=uuid4())
    parent = TokenUsage()
    token = current_usage.set(parent)
    try:
        c.model.fail = True
        with pytest.raises(Exception):
            await agent.run(c.a.business.slug, request, capture_enquiry=False)
        c.model.fail = False
        await agent.run(c.a.business.slug, request, capture_enquiry=False)
        assert current_usage.get() is parent
        assert parent.input_tokens == 24 and parent.output_tokens == 6
        assert not parent.llm_complete
    finally:
        current_usage.reset(token)
    rows = await usage_rows(c)
    assert [row.status for row in rows] == ["failed", "completed"]
    assert [row.tokens_complete for row in rows] == [False, True]
    assert rows[0].request_id == rows[1].request_id


@pytest.mark.parametrize("fails", [False, True])
async def test_messaging_send_timing_status_and_webhook_dedup(chat, monkeypatch, fails):
    import json

    c = chat
    before_send = []

    async def fake_send(*args):
        rows = await usage_rows(c)
        before_send.append(rows[0].duration_ms)
        assert rows[0].status == "awaiting_send"
        await asyncio.sleep(0.02)
        if fails:
            raise RuntimeError("mock send failed")

    monkeypatch.setattr(ADAPTERS["whatsapp"], "send", fake_send)
    raw = json.dumps(wa_payload("PHONE", "USER", "price?", "usage-test-1")).encode()
    services = c.app.state.services
    await process_webhook(services, "whatsapp", c.a.business.slug, {}, raw)
    await process_webhook(services, "whatsapp", c.a.business.slug, {}, raw)
    rows = await usage_rows(c)
    assert len(rows) == 1
    assert rows[0].channel == "whatsapp"
    assert rows[0].status == ("send_failed" if fails else "completed")
    assert rows[0].duration_ms >= before_send[0] + 20
    assert not rows[0].tokens_complete  # Fake model didn't report provider metadata.


async def test_usage_storage_failure_does_not_break_saved_reply(chat, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import SQLAlchemyError

    c = chat
    original = c.db.transaction

    @asynccontextmanager
    async def transaction(tenant=None):
        if tenant is None:
            raise SQLAlchemyError("mock metrics store failure")
        async with original(tenant) as session:
            yield session

    agent = c.app.state.services["agent"]
    monkeypatch.setattr(c.db, "transaction", transaction)
    reply = await agent.run(
        c.a.business.slug, ChatInput(message="price?", request_id=uuid4()), capture_enquiry=False
    )
    assert reply["answer"]
    assert current_usage.get() is None


def test_usage_migration_matches_model_and_preserves_other_tables():
    path = Path(__file__).parents[1] / "migrations/versions/014_ai_usage.py"
    spec = importlib.util.spec_from_file_location("ai_usage_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE businesses (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO businesses VALUES (1)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                actual = {c["name"] for c in inspect(connection).get_columns("ai_usage_records")}
                assert actual == set(AiUsageRecord.__table__.columns.keys()) - {
                    "cached_input_tokens"
                }
                assert len(inspect(connection).get_indexes("ai_usage_records")) == 2
                migration.downgrade()
                assert connection.execute(text("SELECT count(*) FROM businesses")).scalar_one() == 1
                assert inspect(connection).get_table_names() == ["businesses"]
    finally:
        engine.dispose()


async def test_cache_breakdown_distinguishes_historical_unknowns_and_tenants(chat):
    c = chat
    when = datetime(2026, 9, 20, 8, tzinfo=UTC)
    async with c.db.transaction() as session:
        session.add_all(
            [
                record(c.a.business.slug, when, input_tokens=6000, cached_input_tokens=5000),
                record(c.a.business.slug, when, input_tokens=100, cached_input_tokens=0),
                record(c.a.business.slug, when, input_tokens=700),
                record(c.b.business.slug, when, input_tokens=9000, cached_input_tokens=8000),
            ]
        )
    data = (
        await c.client.get(
            endpoint(c.a), headers=c.a.headers, params={"start": "2026-09-20", "end": "2026-09-20"}
        )
    ).json()
    assert data["summary"]["input_tokens"] == 6800
    assert data["summary"]["cached_input_tokens"] == 5000
    assert data["summary"]["uncached_input_tokens"] == 1100
    assert data["summary"]["cache_incomplete_replies"] == 1
    assert data["summary"]["cached_replies"] == 1
    assert data["summary"]["uncached_replies"] == 1
    historical = next(row for row in data["items"] if row["input_tokens"] == 700)
    assert historical["cached_input_tokens"] is None
    assert historical["uncached_input_tokens"] is None
    known = next(row for row in data["items"] if row["input_tokens"] == 6000)
    assert known["cached_input_tokens"] == 5000 and known["uncached_input_tokens"] == 1000


async def test_cache_request_counts_cover_filtered_range_not_current_page_or_model_calls(chat):
    c = chat
    when = datetime(2026, 9, 20, 8, tzinfo=UTC)
    async with c.db.transaction() as session:
        session.add_all(
            [
                record(c.a.business.slug, when, cached_input_tokens=50, llm_calls=3),
                record(c.a.business.slug, when, cached_input_tokens=0, status="send_failed"),
                record(c.a.business.slug, when, tokens_complete=False, status="failed"),
                record(c.a.business.slug, when, "admin_chat", cached_input_tokens=20),
                record(c.a.business.slug, when - timedelta(days=1), cached_input_tokens=30),
                record(c.b.business.slug, when, cached_input_tokens=90),
            ]
        )
    params = {"start": "2026-09-20", "end": "2026-09-20", "limit": 1, "offset": 1}
    data = (await c.client.get(endpoint(c.a), params=params, headers=c.a.headers)).json()
    assert len(data["items"]) == 1
    assert data["summary"]["replies"] == 4
    assert data["summary"]["cached_replies"] == 2  # Three model calls still count as one reply.
    assert data["summary"]["uncached_replies"] == 1
    assert data["summary"]["cache_incomplete_replies"] == 1
    filtered = (
        await c.client.get(
            endpoint(c.a), params={**params, "channel": "instagram"}, headers=c.a.headers
        )
    ).json()["summary"]
    assert filtered["replies"] == 3
    assert filtered["cached_replies"] == 1
    assert filtered["uncached_replies"] == 1
    assert filtered["cache_incomplete_replies"] == 1


def test_context_cache_migration_preserves_historical_usage():
    from context_agent.db import GeminiContextCache

    def load(name):
        path = Path(__file__).parents[1] / "migrations/versions" / name
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    previous, migration = load("014_ai_usage.py"), load("018_gemini_context_cache.py")
    engine = create_engine("sqlite:///:memory:")
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            previous.upgrade()
            connection.execute(
                text("""INSERT INTO ai_usage_records
                (id,tenant_id,request_id,channel,started_at,completed_at,duration_ms,
                 input_tokens,output_tokens,tokens_complete,llm_calls,status)
                VALUES ('old','dental','request','instagram','2026-09-20','2026-09-20',
                        1000,100,20,1,1,'completed')""")
            )
            migration.upgrade()
            for model in [AiUsageRecord, GeminiContextCache]:
                actual = {c["name"] for c in inspect(connection).get_columns(model.__tablename__)}
                assert actual == set(model.__table__.columns.keys())
            assert (
                connection.execute(
                    text("SELECT cached_input_tokens FROM ai_usage_records")
                ).scalar_one()
                is None
            )
            migration.downgrade()
            assert (
                connection.execute(text("SELECT input_tokens FROM ai_usage_records")).scalar_one()
                == 100
            )
            assert "gemini_context_caches" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
