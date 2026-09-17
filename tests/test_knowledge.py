from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FakeModels, FakeVectors
from sqlalchemy import select

from context_agent.db import Document, KnowledgeUnit
from context_agent.knowledge import KnowledgeService
from context_agent.schemas import AddInput, DocumentInput, DomainError, UpdateInput


class AllRetriever:
    settings = SimpleNamespace(candidate_limit=30)

    async def search(self, session, tenant, query, **kwargs):
        return list(
            (
                await session.scalars(
                    select(KnowledgeUnit).where(
                        KnowledgeUnit.tenant_id == tenant, KnowledgeUnit.embedding_status == "ready"
                    )
                )
            ).all()
        )


def extraction(content):
    return {"units": [{"title": "Fee", "content": content}]}


async def test_replacement_keeps_document_id_and_removes_old_units(db):
    models, vectors = FakeModels([extraction("1000"), extraction("1500")]), FakeVectors()
    service = KnowledgeService(db, models, vectors, AllRetriever())
    first, created = await service.put("a", DocumentInput(title="Clinic", summary="1000"))
    old_ids = set(vectors.points)
    second, created_again = await service.put("a", DocumentInput(title="New", summary="1500"))
    assert created and not created_again and first == second
    assert old_ids.isdisjoint(vectors.points)
    async with db.transaction() as session:
        assert len(list((await session.scalars(select(Document))).all())) == 1
        assert (await session.scalar(select(KnowledgeUnit))).content == "1500"


async def test_failed_replacement_preserves_previous_content(db):
    models, vectors = FakeModels([extraction("1000"), extraction("1500")]), FakeVectors()
    service = KnowledgeService(db, models, vectors, AllRetriever())
    await service.put("a", DocumentInput(title="Clinic", summary="1000"))
    vectors.fail = True
    with pytest.raises(RuntimeError):
        await service.put("a", DocumentInput(title="New", summary="1500"))
    async with db.transaction() as session:
        assert (await session.scalar(select(Document))).title == "Clinic"
        assert (await session.scalar(select(KnowledgeUnit))).content == "1000"


async def test_add_requires_existing_document(db):
    service = KnowledgeService(db, FakeModels(), FakeVectors(), AllRetriever())
    with pytest.raises(DomainError) as error:
        await service.add("a", AddInput(content="New fact"))
    assert error.value.code == "document_not_found"


async def test_update_preserves_id_and_does_not_touch_other_tenant(db):
    models = FakeModels([extraction("1000"), extraction("2000")])
    service = KnowledgeService(db, models, FakeVectors(), AllRetriever())
    for tenant in ["a", "b"]:
        await service.put(tenant, DocumentInput(title="Clinic", summary="fee"))
    async with db.transaction() as session:
        row = await session.scalar(select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == "a"))
        target = row.id
    models.replies.append(
        {"outcome": "update", "edits": [{"id": str(target), "title": "Fee", "content": "1500"}]}
    )
    result = await service.update("a", UpdateInput(change="Change fee to 1500"))
    assert result == {"updated_unit_ids": [target]}
    async with db.transaction() as session:
        assert (await session.get(KnowledgeUnit, target)).content == "1500"
        assert (
            await session.scalar(select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == "b"))
        ).content == "2000"


async def test_hallucinated_update_id_rejected(db):
    models = FakeModels(
        [
            extraction("1000"),
            {
                "outcome": "update",
                "edits": [{"id": str(uuid4()), "title": "Fee", "content": "1500"}],
            },
        ]
    )
    service = KnowledgeService(db, models, FakeVectors(), AllRetriever())
    await service.put("a", DocumentInput(title="Clinic", summary="1000"))
    with pytest.raises(DomainError) as error:
        await service.update("a", UpdateInput(change="Change fee"))
    assert error.value.code == "processing_failed"
    async with db.transaction() as session:
        assert (await session.scalar(select(KnowledgeUnit))).content == "1000"


@pytest.mark.parametrize(
    "outcome,code",
    [("not_found", "knowledge_not_found"), ("clarification", "clarification_required")],
)
async def test_update_does_not_create_or_modify_when_unresolved(db, outcome, code):
    models = FakeModels([extraction("1000"), {"outcome": outcome, "clarification": "Which fee?"}])
    service = KnowledgeService(db, models, FakeVectors(), AllRetriever())
    await service.put("a", DocumentInput(title="Clinic", summary="1000"))
    with pytest.raises(DomainError) as error:
        await service.update("a", UpdateInput(change="Change fee"))
    assert error.value.code == code
    async with db.transaction() as session:
        rows = list((await session.scalars(select(KnowledgeUnit))).all())
        assert len(rows) == 1 and rows[0].content == "1000"


async def test_failed_update_index_is_pending_not_ready(db):
    models, vectors = FakeModels([extraction("1000")]), FakeVectors()
    service = KnowledgeService(db, models, vectors, AllRetriever())
    await service.put("a", DocumentInput(title="Clinic", summary="1000"))
    async with db.transaction() as session:
        target = (await session.scalar(select(KnowledgeUnit))).id
    models.replies.append(
        {"outcome": "update", "edits": [{"id": str(target), "title": "Fee", "content": "1500"}]}
    )
    vectors.fail = True
    with pytest.raises(RuntimeError):
        await service.update("a", UpdateInput(change="change"))
    async with db.transaction() as session:
        row = await session.get(KnowledgeUnit, target)
        assert row.content == "1500" and row.embedding_status == "pending"
