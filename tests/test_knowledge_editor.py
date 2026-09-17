from types import SimpleNamespace

import pytest
from conftest import FakeModels, FakeVectors
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import Document, KnowledgeUnit
from context_agent.knowledge import KnowledgeService
from context_agent.schemas import AddInput, DocumentInput, UpdateInput
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business
from super_admin.security import create_token
from super_admin.service import create_account


class Retriever:
    settings = SimpleNamespace(candidate_limit=30)

    async def search(self, session, tenant, query, **kwargs):
        return list(
            (
                await session.scalars(
                    select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == tenant)
                )
            ).all()
        )


def extracted(content):
    return {"units": [{"title": "Opening hours", "content": content}]}


@pytest.fixture
async def editor(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="knowledge-editor-test-secret-32-bytes"
    )
    business, _, _ = await create_business(db, settings, BusinessCreate(name="Test Clinic"))
    async with db.transaction() as session:
        account = await session.scalar(
            select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
        )
    token = create_business_token(BusinessIdentity(account, business), settings)
    models, vectors = FakeModels(), FakeVectors()
    knowledge = KnowledgeService(db, models, vectors, Retriever())
    services = {"db": db, "settings": settings, "knowledge": knowledge}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield SimpleNamespace(
            client=client,
            business=business,
            models=models,
            vectors=vectors,
            knowledge=knowledge,
            settings=settings,
        )


async def load(editor):
    response = await editor.client.get("/admin/knowledge-base")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def save(editor, content, revision):
    return await editor.client.put(
        "/admin/knowledge-base",
        json={
            "content": content,
            "expected_revision": revision,
        },
    )


async def test_editor_saves_original_text_and_prepares_retrievable_units(editor, db):
    first = await load(editor)
    assert first["content"] == "" and not first["reconstructed"]
    editor.models.replies.append(extracted("Open Monday to Friday, 9am–5pm."))
    source = "Opening hours\n\nWe are open Monday to Friday, 9am–5pm."
    response = await save(editor, source, first["revision"])
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["content"] == source and saved["revision"] != first["revision"]
    assert saved == await load(editor)
    async with db.transaction() as session:
        doc = await session.scalar(select(Document))
        row = await session.scalar(select(KnowledgeUnit))
        assert doc.source_text == source and doc.tenant_id == editor.business.slug
        assert row.embedding_status == "ready" and row.document_id == doc.id
        assert ("knowledge_units", str(row.id)) in editor.vectors.points


async def test_failed_replacement_keeps_saved_text_and_knowledge(editor):
    first = await load(editor)
    editor.models.replies.extend([extracted("Old hours"), extracted("New hours")])
    saved = (await save(editor, "Old source", first["revision"])).json()
    editor.vectors.fail = True
    assert (await save(editor, "New source", saved["revision"])).status_code == 502
    assert await load(editor) == saved


async def test_rejects_stale_revision_before_model_calls(editor):
    first = await load(editor)
    editor.models.replies.append(extracted("Current hours"))
    saved = (await save(editor, "Current source", first["revision"])).json()
    response = await save(editor, "Stale source", first["revision"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "knowledge_changed"
    assert await load(editor) == saved


async def test_revision_is_rechecked_after_model_work(editor, db, monkeypatch):
    first = await load(editor)
    editor.models.replies.extend([extracted("Original"), extracted("New")])
    saved = (await save(editor, "Original", first["revision"])).json()
    original_embed = editor.models.embed

    async def concurrent_edit(texts):
        async with db.transaction() as session:
            doc = await session.scalar(select(Document))
            doc.source_text = "Changed in another tab"
        return await original_embed(texts)

    monkeypatch.setattr(editor.models, "embed", concurrent_edit)
    assert (await save(editor, "My stale text", saved["revision"])).status_code == 409
    assert (await load(editor))["content"] == "Changed in another tab"


async def test_legacy_and_incremental_updates_show_current_knowledge(editor, db):
    editor.models.replies.extend([extracted("Old hours"), extracted("Weekend hours")])
    await editor.knowledge.put(
        editor.business.slug, DocumentInput(title="Clinic", summary="Original source")
    )
    async with db.transaction() as session:
        doc = await session.scalar(select(Document))
        doc.source_text = None  # A document created before migration 009.
    legacy = await load(editor)
    assert legacy["reconstructed"] and "Old hours" in legacy["content"]
    await editor.knowledge.add(editor.business.slug, AddInput(content="Weekend hours"))
    added = await load(editor)
    assert "Old hours" in added["content"] and "Weekend hours" in added["content"]
    async with db.transaction() as session:
        row = await session.scalar(
            select(KnowledgeUnit).where(KnowledgeUnit.content == "Old hours")
        )
    editor.models.replies.append(
        {
            "outcome": "update",
            "edits": [
                {"id": str(row.id), "title": "Weekday hours", "content": "Updated hours"},
            ],
        }
    )
    await editor.knowledge.update(editor.business.slug, UpdateInput(change="Update weekday hours"))
    updated = await load(editor)
    assert updated["reconstructed"] and "Updated hours" in updated["content"]
    assert "Old hours" not in updated["content"] and "Weekend hours" in updated["content"]
    assert updated["revision"] != added["revision"] != legacy["revision"]


async def test_incremental_add_invalidates_original_source(editor):
    editor.models.replies.extend([extracted("Old hours"), extracted("New fact")])
    await editor.knowledge.put(
        editor.business.slug, DocumentInput(title="Clinic", summary="Original source")
    )
    await editor.knowledge.add(editor.business.slug, AddInput(content="New fact"))
    value = await load(editor)
    assert value["reconstructed"] and "New fact" in value["content"]
    assert "Original source" not in value["content"]


async def test_incremental_update_invalidates_source_even_when_indexing_fails(editor, db):
    editor.models.replies.append(extracted("Old hours"))
    await editor.knowledge.put(
        editor.business.slug, DocumentInput(title="Clinic", summary="Original source")
    )
    async with db.transaction() as session:
        row = await session.scalar(select(KnowledgeUnit))
    editor.models.replies.append(
        {
            "outcome": "update",
            "edits": [
                {"id": str(row.id), "title": "Hours", "content": "New hours"},
            ],
        }
    )
    editor.vectors.fail = True
    with pytest.raises(RuntimeError):
        await editor.knowledge.update(editor.business.slug, UpdateInput(change="New hours"))
    value = await load(editor)
    assert value["reconstructed"] and "New hours" in value["content"]


async def test_clarification_and_invalid_input_do_not_write(editor):
    first = await load(editor)
    for content in ["", "   ", "x" * 60_001]:
        assert (await save(editor, content, first["revision"])).status_code == 400
    for payload in [
        {"content": "Valid source"},
        {"content": "Valid source", "expected_revision": "bad"},
        {"content": "Valid source", "expected_revision": first["revision"], "tenant_id": "other"},
    ]:
        assert (await editor.client.put("/admin/knowledge-base", json=payload)).status_code == 400
    editor.models.replies.append({"units": [], "clarification": "Which opening hours apply?"})
    response = await save(editor, "Ambiguous source", first["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Which opening hours apply?"
    assert await load(editor) == first


async def test_only_business_owner_can_read_and_write_its_knowledge(editor, db):
    first = await load(editor)
    admin = await create_account(db, "platform-admin", "a-long-test-only-password")
    for auth in ["", "Bearer invalid", f"Bearer {create_token(admin, editor.settings)}"]:
        assert (
            await editor.client.get("/admin/knowledge-base", headers={"Authorization": auth})
        ).status_code == 401
        assert (
            await editor.client.put(
                "/admin/knowledge-base",
                headers={"Authorization": auth},
                json={
                    "content": "Other source",
                    "expected_revision": first["revision"],
                },
            )
        ).status_code == 401
    editor.models.replies.append(extracted("Private clinic facts"))
    saved = (await save(editor, "Private clinic source", first["revision"])).json()
    other, username, password = await create_business(
        db, editor.settings, BusinessCreate(name="Other Business")
    )
    login = await editor.client.post(
        "/auth/login",
        json={"business_slug": other.slug, "username": username, "password": password},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    result = await editor.client.get(
        "/admin/knowledge-base", headers=other_headers, params={"tenant_id": editor.business.slug}
    )
    assert result.status_code == 200 and result.json()["content"] == ""
    assert (
        await editor.client.put(
            "/admin/knowledge-base",
            headers=other_headers,
            json={
                "content": "Other business source",
                "expected_revision": saved["revision"],
            },
        )
    ).status_code == 409
    assert await load(editor) == saved
