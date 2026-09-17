from collections import deque

import pytest

from context_agent.db import Base, Database


class FakeModels:
    def __init__(self, replies=()):
        self.replies = deque(replies)
        self.embedded = []

    async def structured(self, schema, instruction, data):
        reply = self.replies.popleft()
        return schema.model_validate(reply)

    async def embed(self, texts):
        self.embedded.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def query(self, text):
        return [1.0, 0.0, 0.0]


class FakeVectors:
    def __init__(self):
        self.points = {}
        self.fail = False
        self.leak = False

    async def upsert(self, kind, rows, embeddings):
        if self.fail:
            raise RuntimeError("injected index failure")
        for row, vector in zip(rows, embeddings, strict=True):
            self.points[(kind, str(row.id))] = (row.tenant_id, vector)

    async def similarities(self, kind, query, tenant, ids):
        return {key: 1.0 for key in ids if (kind, key) in self.points}

    async def delete(self, kind, ids):
        for key in ids:
            self.points.pop((kind, str(key)), None)

    async def search(self, kind, vector, tenant, limit):
        return [
            key
            for (collection, key), (scope, _) in self.points.items()
            if collection == kind
            and (self.leak or scope == tenant or (kind == "tools" and scope == "general"))
        ][:limit]


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.close()
