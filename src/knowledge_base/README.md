# Business knowledge editor

`GET /admin/knowledge-base` reads the signed-in business's saved knowledge.
`PUT /admin/knowledge-base` accepts `content` (1–60,000 nonblank characters) and
`expected_revision` from the GET response. Both use business-admin authentication;
the business slug comes from the verified account. Responses disable caching.

The frontend at `/knowledge` uses the same single-editor layout as AI Instructions.
Saving replaces the business's knowledge document through `KnowledgeService.put`:
fact extraction, embedding, vector staging, and canonical database replacement.
Only successful saves show a success state. Empty submissions are rejected; this
editor does not provide a clear-all operation. Saving uses the configured model
and embedding provider and can return clarification or processing errors.

Migration `009` adds nullable `documents.source_text`. Successful document saves
retain the submitted source so it can be edited without reconstructing text from
extracted facts. Existing documents load their current knowledge units as one
document. Incremental add/update operations invalidate the original source, so
the editor shows current facts instead of stale text. No knowledge is changed by
loading the page or applying this migration.

The revision fingerprints the document and its knowledge units. It is checked
before model work and again under the tenant transaction lock before replacement.
Concurrent changes receive HTTP 409. A failed replacement preserves the preceding
canonical knowledge and source; the existing vector reconciliation rules apply.
Legacy `/api/v1` routes retain their existing authorization behavior.

Rebuild locally with `docker compose up -d --build agent`, or run
`uv run alembic upgrade head` before starting a host API process.

```bash
uv run pytest -q tests/test_knowledge_editor.py tests/test_knowledge_source_migration.py tests/test_knowledge.py
uv run ruff check src tests migrations
```

Tests use fake extraction/embedding providers and isolated SQLite storage. They
cover saved source text, ready knowledge units, legacy compatibility, incremental
changes, stale writes, index failures, validation, owner isolation, and migration
round trips. They do not measure extraction quality or PostgreSQL concurrency.
