"""Single-instance local container initialization, followed by the API server."""

import asyncio
import logging
import os
import subprocess
import time

import httpx
from sqlalchemy import select, text

from .cli import reconcile
from .config import Settings
from .db import Database, Document, KnowledgeUnit
from .models import Models
from .tools import REGISTRY, sync_tools
from .vectors import Vectors

log = logging.getLogger(__name__)


async def wait_for_storage(settings, timeout=90):
    db = Database(settings.database_url)
    deadline = time.monotonic() + timeout
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    async with db.transaction() as session:
                        await session.execute(text("SELECT 1"))
                    response = await client.get(settings.qdrant_url.rstrip("/") + "/readyz")
                    response.raise_for_status()
                    return
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("PostgreSQL or Qdrant did not become ready") from None
                    await asyncio.sleep(2)
    finally:
        await db.close()


async def missing_vectors(vectors, kind, ids):
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        points = await vectors.client.retrieve(
            vectors.collection(kind),
            ids=[str(item) for item in batch],
            with_payload=False,
            with_vectors=False,
        )
        if len(points) != len(batch):
            return True
    return False


async def initialize(settings):
    db, vectors = Database(settings.database_url), Vectors(settings)
    models = None
    try:
        models = Models(settings)
        await vectors.initialize()
        # Reconcile persisted knowledge on startup, including recovery from partial initialization.
        async with db.transaction() as session:
            tenants = list((await session.scalars(select(Document.tenant_id))).all())
        for tenant in tenants:
            async with db.transaction() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == tenant)
                        )
                    ).all()
                )
            pending = any(row.embedding_status != "ready" for row in rows)
            if pending or await missing_vectors(vectors, "knowledge_units", [r.id for r in rows]):
                await reconcile(db, models, vectors, tenant)
        # A fresh Qdrant volume may coexist with ready PostgreSQL tool rows.
        rebuild_tools = await missing_vectors(vectors, "tools", [tool.id for tool in REGISTRY])
        await sync_tools(db, models, vectors, force_reembed=rebuild_tools)
    finally:
        if models is not None:
            await models.close()
        await vectors.close()
        await db.close()


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.gemini_api_key or not settings.gemini_api_key.get_secret_value():
        raise RuntimeError("Set GEMINI_API_KEY before starting the stack")
    log.info("Waiting for PostgreSQL and Qdrant")
    asyncio.run(wait_for_storage(settings))
    log.info("Applying database migrations")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    log.info("Initializing indexes and syncing knowledge/tools")
    asyncio.run(initialize(settings))
    log.info("Starting API on port 8000")
    os.execvp(
        "uvicorn", ["uvicorn", "context_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
    )


if __name__ == "__main__":
    main()
