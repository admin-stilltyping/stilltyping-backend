from unittest.mock import AsyncMock
from uuid import uuid4

from conftest import FakeModels
from qdrant_client import AsyncQdrantClient

from context_agent import bootstrap
from context_agent.config import Settings
from context_agent.db import Document, KnowledgeUnit
from context_agent.tools import SUPPORT
from context_agent.vectors import Vectors


async def test_bootstrap_recovers_missing_vectors_and_reuses_ready_data(db, monkeypatch):
    settings = Settings(_env_file=None, embedding_dimensions=3)
    vectors = Vectors(settings)
    await vectors.client.close()
    vectors.client = AsyncQdrantClient(location=":memory:")
    models = FakeModels()
    models.close = AsyncMock()
    close_db, close_vectors = db.close, vectors.close
    monkeypatch.setattr(db, "close", AsyncMock())
    monkeypatch.setattr(vectors, "close", AsyncMock())
    monkeypatch.setattr(bootstrap, "Database", lambda _: db)
    monkeypatch.setattr(bootstrap, "Vectors", lambda _: vectors)
    monkeypatch.setattr(bootstrap, "Models", lambda _: models)
    doc_id, unit_id = uuid4(), uuid4()
    async with db.transaction() as session:
        session.add(Document(id=doc_id, tenant_id="a", title="Example"))
        await session.flush()
        session.add(
            KnowledgeUnit(
                id=unit_id,
                document_id=doc_id,
                tenant_id="a",
                title="Hours",
                content="Open at 9",
                content_hash="hash",
                embedding_status="ready",
            )
        )
    try:
        await bootstrap.initialize(settings)
        assert len(models.embedded) == 2  # Knowledge and support tool recovered.
        await bootstrap.initialize(settings)
        assert len(models.embedded) == 2  # Restart consumes no embeddings.
        await vectors.delete("tools", [SUPPORT.id])
        await bootstrap.initialize(settings)
        assert len(models.embedded) == 3
        await vectors.delete("knowledge_units", [unit_id])
        await bootstrap.initialize(settings)
        assert len(models.embedded) == 4
    finally:
        await close_vectors()
        await close_db()
