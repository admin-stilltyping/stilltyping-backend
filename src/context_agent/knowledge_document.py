"""Editable knowledge snapshots, including legacy documents without source text."""

import hashlib
import json

from sqlalchemy import select

from .db import Document, KnowledgeUnit
from .schemas import DomainError


async def document_snapshot(session, tenant):
    doc = await session.scalar(select(Document).where(Document.tenant_id == tenant))
    rows = []
    if doc is not None:
        rows = list(
            (
                await session.scalars(
                    select(KnowledgeUnit)
                    .where(KnowledgeUnit.tenant_id == tenant, KnowledgeUnit.document_id == doc.id)
                    .order_by(KnowledgeUnit.created_at, KnowledgeUnit.id)
                )
            ).all()
        )
    reconstructed = doc is not None and doc.source_text is None
    content = (
        "\n\n".join(f"{row.title}\n{row.content}" for row in rows)
        if reconstructed
        else (doc.source_text or "")
        if doc
        else ""
    )
    fingerprint = {
        "document": str(doc.id) if doc else None,
        "title": doc.title if doc else None,
        "content": content,
        "units": [(str(row.id), row.title, row.content) for row in rows],
    }
    return {
        "content": content,
        "revision": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest(),
        "reconstructed": reconstructed,
    }


async def check_document_revision(session, tenant, expected):
    if expected is not None and (await document_snapshot(session, tenant))["revision"] != expected:
        raise DomainError(
            409,
            "knowledge_changed",
            "Knowledge was updated elsewhere. Load the saved version before saving again.",
        )
