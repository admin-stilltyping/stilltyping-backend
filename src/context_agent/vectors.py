import math
from functools import wraps

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from .schemas import DomainError


def storage_errors(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except (ResponseHandlingException, UnexpectedResponse) as exc:
            raise DomainError(503, "storage_unavailable", "Vector storage is unavailable.") from exc

    return wrapped


class Vectors:
    def __init__(self, settings):
        self.client = AsyncQdrantClient(
            url=settings.qdrant_url,
            timeout=30,
            api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        )
        self.prefix = settings.index_prefix
        self.dimensions = settings.embedding_dimensions

    def collection(self, kind):
        return f"{self.prefix}_{kind}"

    @storage_errors
    async def initialize(self):
        for kind in ("knowledge_units", "tools"):
            name = self.collection(kind)
            if not await self.client.collection_exists(name):
                await self.client.create_collection(
                    name,
                    vectors_config=models.VectorParams(
                        size=self.dimensions, distance=models.Distance.COSINE
                    ),
                )
                await self.client.create_payload_index(
                    name, "tenant_id", models.PayloadSchemaType.KEYWORD
                )
            info = await self.client.get_collection(name)
            if info.config.params.vectors.size != self.dimensions:
                raise RuntimeError("Vector dimensions changed: use a new INDEX_PREFIX and reindex.")

    @storage_errors
    async def upsert(self, kind, rows, embeddings):
        if rows:
            await self.client.upsert(
                self.collection(kind),
                points=[
                    models.PointStruct(
                        id=str(row.id), vector=vector, payload={"tenant_id": row.tenant_id}
                    )
                    for row, vector in zip(rows, embeddings, strict=True)
                ],
                wait=True,
            )

    @storage_errors
    async def search(self, kind, vector, tenant, limit):
        scopes = [tenant, "general"] if kind == "tools" else [tenant]
        result = await self.client.query_points(
            self.collection(kind),
            query=vector,
            limit=limit,
            query_filter=models.Filter(
                must=[models.FieldCondition(key="tenant_id", match=models.MatchAny(any=scopes))]
            ),
            with_payload=False,
        )
        return [str(point.id) for point in result.points]

    @storage_errors
    async def similarities(self, kind, query, tenant, ids):
        """Score the union of dense and lexical candidates in the same embedding space."""
        scores = {}
        scopes = {tenant, "general"} if kind == "tools" else {tenant}
        query_norm = math.sqrt(sum(x * x for x in query))
        if not query_norm:
            return scores
        for start in range(0, len(ids), 100):
            points = await self.client.retrieve(
                self.collection(kind),
                ids=ids[start : start + 100],
                with_vectors=True,
                with_payload=True,
            )
            for point in points:
                vector = point.vector
                if (point.payload or {}).get("tenant_id") not in scopes:
                    continue
                if not isinstance(vector, list) or len(vector) != len(query):
                    continue
                norm = math.sqrt(sum(x * x for x in vector))
                if norm:
                    scores[str(point.id)] = sum(a * b for a, b in zip(query, vector)) / (
                        query_norm * norm
                    )
        return scores

    @storage_errors
    async def delete(self, kind, ids):
        if ids:
            await self.client.delete(
                self.collection(kind),
                points_selector=models.PointIdsList(points=[str(x) for x in ids]),
                wait=True,
            )

    async def close(self):
        await self.client.close()
