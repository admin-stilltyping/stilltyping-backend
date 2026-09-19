from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FakeModels, FakeVectors
from sqlalchemy import select

from context_agent.db import Tool
from context_agent.tools import REGISTRY, SUPPORT, ToolContext, available_tools, execute, sync_tools


async def test_sync_skips_unchanged_embeddings(db):
    models, vectors = FakeModels(), FakeVectors()
    await sync_tools(db, models, vectors)
    first_points = dict(vectors.points)
    await sync_tools(db, models, vectors)
    assert len(models.embedded) == len(REGISTRY) and first_points == vectors.points
    async with db.transaction() as session:
        assert len(list((await session.scalars(select(Tool))).all())) == len(REGISTRY)


async def test_sync_reembeds_description_change(db):
    models, vectors = FakeModels(), FakeVectors()
    await sync_tools(db, models, vectors)
    changed = replace(SUPPORT, description="Updated description")
    await sync_tools(db, models, vectors, [changed])
    assert len(models.embedded) == len(REGISTRY) + 1
    assert changed.id == SUPPORT.id


async def test_support_creates_internal_ticket_without_webhook(db):
    from context_agent import support

    ctx = ToolContext(
        "a", uuid4(), "call", SimpleNamespace(support_webhook_url=None), db=db, channel="web"
    )
    result = await execute(SUPPORT, {"question": "Q?", "reason": "No evidence"}, ctx)
    assert result["ok"] and result["ticket_id"].startswith("TKT-")
    async with db.transaction("a") as session:
        rows = await support.list_tickets(session, "a")
    assert len(rows) == 1 and rows[0].status == "open"


async def test_support_is_idempotent_on_retry(db):
    from context_agent import support

    request_id = uuid4()
    settings = SimpleNamespace(support_webhook_url=None)
    for _ in range(2):
        await execute(
            SUPPORT,
            {"question": "Q?", "reason": "No evidence"},
            ToolContext("a", request_id, "call", settings, db=db),
        )
    async with db.transaction("a") as session:
        rows = await support.list_tickets(session, "a")
    assert len(rows) == 1


async def test_schema_rejects_model_supplied_tenant_override():
    with pytest.raises(Exception):
        await execute(
            SUPPORT,
            {"question": "?", "reason": "?", "tenant_id": "victim"},
            ToolContext("a", uuid4(), "call", SimpleNamespace(support_webhook_url=None)),
        )


async def test_removed_registry_tool_cannot_be_loaded(db):
    models, vectors = FakeModels(), FakeVectors()
    custom = replace(SUPPORT, name="custom", handler_key="custom")
    await sync_tools(db, models, vectors, [custom])
    async with db.transaction() as session:
        row = await session.get(Tool, custom.id)
        result = await available_tools(session, "a", [row], registry=[])
    assert list(result) == [SUPPORT.name]


async def test_sync_rebuilds_unchanged_tools_in_new_index(db):
    models = FakeModels()
    await sync_tools(db, models, FakeVectors())
    new_index = FakeVectors()
    await sync_tools(db, models, new_index, force_reembed=True)
    assert len(models.embedded) == len(REGISTRY) * 2
    assert len(new_index.points) == len(REGISTRY)
