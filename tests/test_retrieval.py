from types import SimpleNamespace
from uuid import uuid4

from conftest import FakeModels, FakeVectors

from context_agent.db import Document, KnowledgeUnit, content_hash
from context_agent.retrieval import Retriever, fuse


async def test_canonical_tenant_check_rejects_bad_vector_result(db):
    vectors = FakeVectors()
    vectors.leak = True
    async with db.transaction() as session:
        for tenant in ["a", "b"]:
            doc = Document(id=uuid4(), tenant_id=tenant, title="Clinic")
            session.add(doc)
            await session.flush()
            row = KnowledgeUnit(
                id=uuid4(),
                tenant_id=tenant,
                document_id=doc.id,
                title="Fee",
                content="1000",
                content_hash=content_hash("Fee", "1000"),
                embedding_status="ready",
            )
            session.add(row)
            await vectors.upsert("knowledge_units", [row], [[1, 0, 0]])
    settings = SimpleNamespace(
        candidate_limit=30, knowledge_limit=4, tool_limit=5, relevance_threshold=0.70
    )
    models = FakeModels([{"ids": []}])
    retriever = Retriever(models, vectors, settings)
    async with db.transaction("a") as session:
        result = await retriever.search(session, "a", "fee")
    assert len(result) == 1 and result[0].tenant_id == "a"


def test_rrf_favors_agreement_without_duplicating_ids():
    assert fuse(["a", "b", "b"], ["b", "c"])[0] == "b"
    assert len(fuse(["a", "b", "b"], ["b", "c"])) == 3


async def test_filter_union_including_lexical_only_before_limit(db):
    class SearchVectors(FakeVectors):
        async def search(self, *args):
            return [str(weak.id), str(dense.id)]

        async def similarities(self, kind, vector, tenant, ids):
            assert set(ids) == {str(weak.id), str(dense.id), str(lexical.id)}
            return {str(weak.id): 0.2, str(dense.id): 0.8, str(lexical.id): 0.70}

    async with db.transaction() as session:
        doc = Document(id=uuid4(), tenant_id="a", title="Clinic")
        session.add(doc)
        await session.flush()
        rows = [
            KnowledgeUnit(
                id=uuid4(),
                tenant_id="a",
                document_id=doc.id,
                title="fact",
                content=content,
                content_hash="hash",
                embedding_status="ready",
            )
            for content in ["irrelevant", "semantic fact", "needle exact match"]
        ]
        weak, dense, lexical = rows
        session.add_all(rows)
    models = FakeModels()  # Any reranking call would fail: no fake LLM replies.
    config = SimpleNamespace(
        candidate_limit=30, knowledge_limit=4, tool_limit=5, relevance_threshold=0.70
    )
    async with db.transaction() as session:
        found = await Retriever(models, SearchVectors(), config).search(
            session, "a", "needle", vector=[1, 0, 0]
        )
    assert [row.id for row in found] == [lexical.id, dense.id]
