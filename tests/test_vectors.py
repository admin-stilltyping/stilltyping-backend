from types import SimpleNamespace
from uuid import uuid4

from qdrant_client import AsyncQdrantClient

from context_agent.vectors import Vectors


async def test_real_qdrant_local_index_filters_scopes():
    settings = SimpleNamespace(
        qdrant_url="http://localhost:6333",
        qdrant_api_key=None,
        index_prefix="test",
        embedding_dimensions=3,
    )
    vectors = Vectors(settings)
    await vectors.client.close()
    vectors.client = AsyncQdrantClient(location=":memory:")
    try:
        await vectors.initialize()
        a = SimpleNamespace(id=uuid4(), tenant_id="a")
        b = SimpleNamespace(id=uuid4(), tenant_id="b")
        shared = SimpleNamespace(id=uuid4(), tenant_id="general")
        await vectors.upsert("knowledge_units", [a, b], [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        await vectors.upsert("tools", [a, b, shared], [[1.0, 0.0, 0.0]] * 3)
        assert await vectors.search("knowledge_units", [1.0, 0.0, 0.0], "a", 10) == [str(a.id)]
        assert set(await vectors.search("tools", [1.0, 0.0, 0.0], "a", 10)) == {
            str(a.id),
            str(shared.id),
        }
        scores = await vectors.similarities(
            "knowledge_units", [2, 0, 0], "a", [str(a.id), str(b.id)]
        )
        assert scores == {str(a.id): 1.0}
        assert await vectors.similarities("knowledge_units", [0, 0, 0], "a", [str(a.id)]) == {}
        await vectors.delete("knowledge_units", [a.id])
        assert await vectors.search("knowledge_units", [1.0, 0.0, 0.0], "a", 10) == []
    finally:
        await vectors.close()
