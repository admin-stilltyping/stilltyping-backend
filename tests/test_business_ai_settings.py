import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import Document, KnowledgeUnit, content_hash
from context_agent.models import Models
from context_agent.schemas import DomainError
from integrations import ai_settings, runtime
from integrations.gemini import decrypt_key
from integrations.models import BusinessAISettings
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business
from super_admin.service import create_account


@pytest.fixture
async def setup(db, monkeypatch):
    settings = Settings(
        _env_file=None,
        gemini_api_key="platform-test-secret",
        super_admin_jwt_secret="test-only-secret-at-least-32-characters-long",
        integration_encryption_key=Fernet.generate_key().decode(),
    )
    a, _, _ = await create_business(db, settings, BusinessCreate(name="Business A"))
    b, username, password = await create_business(db, settings, BusinessCreate(name="Business B"))
    from conftest import FakeVectors

    vectors = FakeVectors()
    vectors.prefix = settings.index_prefix
    vectors.initialize = AsyncMock()
    models = SimpleNamespace(
        query=AsyncMock(return_value=[1] * 32),
        embed=AsyncMock(side_effect=lambda texts: [[1] * 32 for _ in texts]),
        close=AsyncMock(),
    )
    monkeypatch.setattr(ai_settings, "Models", lambda settings: models)
    monkeypatch.setattr(ai_settings, "validate_llm", AsyncMock())
    services = dict(db=db, settings=settings, vectors=vectors)
    app = create_app(services)
    app.state.services = services
    await create_account(db, "platform", "test-only-password")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = (
            await client.post(
                "/auth/super-admin/login",
                json={"username": "platform", "password": "test-only-password"},
            )
        ).json()["access_token"]
        owner = (
            await client.post(
                "/auth/login",
                json={"business_slug": b.slug, "username": username, "password": password},
            )
        ).json()["access_token"]
        yield SimpleNamespace(
            db=db,
            settings=settings,
            a=a,
            b=b,
            client=client,
            headers={"Authorization": f"Bearer {token}"},
            owner={"Authorization": f"Bearer {owner}"},
            models=models,
            vectors=vectors,
        )


def payload(**changes):
    return dict(
        revision="",
        llm_provider="deepseek",
        llm_model="deepseek-flash",
        llm_api_key="deepseek-test-secret",
        embedding_provider="deepinfra",
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_dimensions=32,
        embedding_api_key="deepinfra-test-secret",
        relevance_threshold=0.6,
        **changes,
    )


def path(c, business=None):
    return f"/super-admin/businesses/{(business or c.a).slug}/ai-settings"


async def test_auth_and_secret_isolation(setup):
    c = setup
    assert (await c.client.get(path(c))).status_code == 401
    assert (await c.client.put(path(c), headers=c.owner, json=payload())).status_code == 401
    r = await c.client.put(path(c), headers=c.headers, json=payload())
    assert r.status_code == 200, r.text
    assert "test-secret" not in r.text
    assert r.headers["cache-control"] == "no-store"
    async with c.db.transaction() as session:
        row = await session.get(BusinessAISettings, c.a.id)
        assert "test-secret" not in row.encrypted_key
        assert json.loads(decrypt_key(c.settings, row))["llm"] == "deepseek-test-secret"
        assert row.index_prefix.startswith("business_")
    other = await c.client.get(path(c, c.b), headers=c.headers)
    assert other.json()["source"] == "inherited"
    assert "llm_key_last_four" not in other.json()
    stale = await c.client.put(path(c), headers=c.headers, json=payload())
    assert stale.status_code == 409


async def test_failed_reindex_preserves_active_config(setup):
    c = setup
    c.models.query.side_effect = DomainError(503, "model_unavailable", "Unavailable")
    result = await c.client.put(path(c), headers=c.headers, json=payload())
    assert result.status_code == 503
    async with c.db.transaction() as session:
        assert await session.get(BusinessAISettings, c.a.id) is None
    c.models.close.assert_awaited()


async def test_key_rotation_keeps_index_and_blank_retains_keys(setup):
    c = setup
    first = (await c.client.put(path(c), headers=c.headers, json=payload())).json()
    async with c.db.transaction() as session:
        prefix = (await session.get(BusinessAISettings, c.a.id)).index_prefix
    c.vectors.initialize.reset_mock()
    update = payload()
    update.update(
        revision=first["revision"], llm_api_key=None, embedding_api_key="replacement-secret"
    )
    r = await c.client.put(path(c), headers=c.headers, json=update)
    assert r.status_code == 200, r.text
    c.vectors.initialize.assert_not_awaited()
    async with c.db.transaction() as session:
        row = await session.get(BusinessAISettings, c.a.id)
        assert row.index_prefix == prefix
        keys = json.loads(decrypt_key(c.settings, row))
        assert keys == {"llm": "deepseek-test-secret", "embedding": "replacement-secret"}


async def test_knowledge_changed_during_rebuild_does_not_activate(setup):
    c = setup

    async def mutate(_):
        from uuid import uuid4

        async with c.db.transaction(c.a.slug) as session:
            doc = Document(id=uuid4(), tenant_id=c.a.slug, title="Clinic")
            session.add(doc)
            await session.flush()
            session.add(
                KnowledgeUnit(
                    tenant_id=c.a.slug,
                    document_id=doc.id,
                    title="Hours",
                    content="9 AM",
                    content_hash=content_hash("Hours", "9 AM"),
                    embedding_status="ready",
                )
            )
        return [1] * 32

    c.models.query.side_effect = mutate
    r = await c.client.put(path(c), headers=c.headers, json=payload())
    assert r.status_code == 409
    async with c.db.transaction() as session:
        assert await session.get(BusinessAISettings, c.a.id) is None


async def test_runtime_resolves_business_settings(setup, monkeypatch):
    c = setup
    assert (await c.client.put(path(c), headers=c.headers, json=payload())).status_code == 200
    resolved = []

    def factory(settings):
        resolved.append(settings)
        return SimpleNamespace(close=AsyncMock())

    monkeypatch.setattr(runtime, "Models", factory)
    default = object()
    ai = runtime.BusinessAI(c.db, c.vectors, c.settings, None)
    ai.default_models = default
    async with ai.models(c.a.slug) as selected:
        assert selected is not default
        assert selected.tenant_vectors.prefix.startswith("business_")
    assert resolved[0].llm_api_key.get_secret_value() == "deepseek-test-secret"
    assert resolved[0].embedding_api_key.get_secret_value() == "deepinfra-test-secret"
    async with ai.models(c.b.slug) as selected:
        assert selected is default


@pytest.mark.parametrize("provider", ["deepseek", "deepinfra"])
async def test_deepinfra_adapter_usage_and_prompts(monkeypatch, provider):
    from context_agent.usage import TokenUsage, current_usage

    settings = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_api_key="test-llm",
        embedding_provider="deepinfra",
        embedding_api_key="test-embed",
        chat_model="deepseek-flash",
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_dimensions=32,
    )
    models = Models(settings)
    call = AsyncMock(
        return_value=SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * 32)], usage=SimpleNamespace(prompt_tokens=7)
        )
    )
    monkeypatch.setattr(models.embeddings.embeddings, "create", call)
    assert not hasattr(models, "cache_credential_fingerprint")
    usage = TokenUsage()
    token = current_usage.set(usage)
    try:
        await models.embed(["clinic hours"])
        await models.query("eppo open?")
        assert call.await_args_list[0].kwargs["input"] == ["clinic hours"]
        assert call.await_args_list[1].kwargs["input"][0].startswith("Instruct:")
        assert models.chat.openai_api_base == (
            "https://api.deepinfra.com/v1/openai"
            if provider == "deepinfra"
            else "https://api.deepseek.com"
        )
        assert models.chat.extra_body == (
            None if provider == "deepinfra" else {"thinking": {"type": "disabled"}}
        )
        assert usage.embedding_tokens == 14
        assert usage.embedding_calls == 2
    finally:
        current_usage.reset(token)
        await models.close()


async def test_old_knowledge_writer_is_rejected_after_switch(setup):
    from context_agent.knowledge import KnowledgeService

    c = setup
    assert (await c.client.put(path(c), headers=c.headers, json=payload())).status_code == 200
    service = KnowledgeService(c.db, c.models, c.vectors, None)
    async with c.db.transaction(c.a.slug) as session:
        with pytest.raises(DomainError, match="AI settings changed"):
            await service.check_index(session, c.a.slug)
    # Another business is unaffected.
    async with c.db.transaction(c.b.slug) as session:
        await service.check_index(session, c.b.slug)


async def test_reindex_contains_only_business_knowledge_and_shared_tools(setup):
    from uuid import uuid4

    from context_agent.db import Tool

    c = setup
    async with c.db.transaction() as session:
        for business in (c.a, c.b):
            doc = Document(id=uuid4(), tenant_id=business.slug, title="Source")
            session.add(doc)
            await session.flush()
            session.add(
                KnowledgeUnit(
                    id=uuid4(),
                    tenant_id=business.slug,
                    document_id=doc.id,
                    title="Hours",
                    content=business.slug,
                    content_hash=content_hash("Hours", business.slug),
                    embedding_status="ready",
                )
            )
        for tenant in (c.a.slug, c.b.slug, "general"):
            session.add(
                Tool(
                    id=uuid4(),
                    tenant_id=tenant,
                    name="test_tool",
                    description=tenant,
                    parameters_schema={},
                    handler_key="test",
                    embedding_status="ready",
                )
            )
    r = await c.client.put(path(c), headers=c.headers, json=payload())
    assert r.status_code == 200, r.text
    texts = [text for call in c.models.embed.await_args_list for text in call.args[0]]
    assert any(c.a.slug in text for text in texts)
    assert any("general" in text for text in texts)
    assert not any(c.b.slug in text for text in texts)


async def test_deepinfra_llm_shared_key(setup):
    c = setup
    data = payload()
    data.update(
        llm_provider="deepinfra",
        llm_model="deepseek-ai/DeepSeek-V4-Flash-0731",
        llm_api_key="shared-deepinfra-key",
        embedding_api_key="shared-deepinfra-key",
    )
    result = await c.client.put(path(c), headers=c.headers, json=data)
    assert result.status_code == 200, result.text
    assert result.json()["llm_provider"] == "deepinfra"
    assert "shared-deepinfra-key" not in result.text
    async with c.db.transaction() as session:
        row = await session.get(BusinessAISettings, c.a.id)
        keys = json.loads(decrypt_key(c.settings, row))
        assert keys["llm"] == keys["embedding"] == "shared-deepinfra-key"


async def test_deepinfra_model_validation_uses_hosted_catalog(monkeypatch):
    seen = []
    real_client = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "deepseek-ai/example"}]})

    monkeypatch.setattr(
        ai_settings.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    await ai_settings.validate_llm(
        {"llm_provider": "deepinfra", "llm_model": "deepseek-ai/example"}, "test-key"
    )
    assert str(seen[0].url) == "https://api.deepinfra.com/v1/openai/models"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    with pytest.raises(DomainError, match="not available"):
        await ai_settings.validate_llm(
            {"llm_provider": "deepinfra", "llm_model": "missing"}, "test-key"
        )
