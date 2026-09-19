import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.schemas import DomainError
from integrations import gemini, runtime
from integrations.models import BusinessGeminiCredential
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business

KEY_A = "test-gemini-credential-business-a-1234"
KEY_B = "test-gemini-credential-business-b-5678"


@pytest.fixture
async def integration(db, monkeypatch):
    settings = Settings(
        _env_file=None,
        super_admin_jwt_secret="gemini-test-only-signing-secret-at-least-32-characters",
        integration_encryption_key=Fernet.generate_key().decode(),
        gemini_api_key="test-platform-key",
    )
    owners = []
    for name in ("Gemini Test A", "Gemini Test B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            admin = await session.scalar(
                select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
            )
        owners.append(
            SimpleNamespace(
                business=business,
                headers={
                    "Authorization": "Bearer "
                    + create_business_token(BusinessIdentity(admin, business), settings)
                },
            )
        )
    verify = AsyncMock()
    monkeypatch.setattr(gemini, "validate_key", verify)
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            db=db,
            settings=settings,
            app=app,
            client=client,
            a=owners[0],
            b=owners[1],
            verify=verify,
        )


def url(owner):
    return f"/admin/{owner.business.slug}/integrations/gemini"


async def save(c, owner=None, key=KEY_A):
    owner = owner or c.a
    return await c.client.put(url(owner), headers=owner.headers, json={"api_key": key})


async def test_save_update_encrypted_and_never_echo_key(integration):
    c = integration
    before = await c.client.get(url(c.a), headers=c.a.headers)
    assert before.json()["source"] == "platform"
    assert before.json()["key_last_four"] is None
    response = await save(c)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["source"] == "business"
    assert response.json()["key_last_four"] == "1234"
    assert KEY_A not in response.text
    c.verify.assert_awaited_once_with(KEY_A, c.settings)
    async with c.db.transaction() as session:
        row = await session.get(BusinessGeminiCredential, c.a.business.id)
        original = row.encrypted_key
        assert KEY_A not in original
        assert gemini.decrypt_key(c.settings, row) == KEY_A
    response = await save(c, key=KEY_B)
    assert response.json()["key_last_four"] == "5678"
    async with c.db.transaction() as session:
        row = await session.get(BusinessGeminiCredential, c.a.business.id)
        assert row.encrypted_key != original
        assert gemini.decrypt_key(c.settings, row) == KEY_B
    response = await c.client.get(url(c.a), headers=c.a.headers)
    assert KEY_B not in response.text
    assert response.json()["updated_at"]


async def test_only_owner_can_read_or_update(integration):
    c = integration
    assert (await c.client.get(url(c.a))).status_code == 401
    assert (await c.client.get(url(c.a), headers=c.b.headers)).status_code == 403
    response = await c.client.put(url(c.a), headers=c.b.headers, json={"api_key": KEY_A})
    assert response.status_code == 403
    c.verify.assert_not_awaited()
    await save(c)
    other = await c.client.get(url(c.b), headers=c.b.headers)
    assert other.json()["source"] == "platform"
    assert other.json()["key_last_four"] is None


async def test_rejected_update_preserves_saved_key_and_no_secret_errors(integration):
    c = integration
    await save(c)
    c.verify.side_effect = DomainError(400, "invalid_gemini_key", "Gemini rejected this key.")
    assert (await save(c, key=KEY_B)).status_code == 400
    for key in ("", "bad", "a" * 257, "bad\n" + KEY_A):
        response = await save(c, key=key)
        assert response.status_code == 400
        assert KEY_A not in response.text
    async with c.db.transaction() as session:
        assert (
            gemini.decrypt_key(
                c.settings, await session.get(BusinessGeminiCredential, c.a.business.id)
            )
            == KEY_A
        )


async def test_storage_key_required_and_owner_binding(integration):
    c = integration
    await save(c)
    async with c.db.transaction() as session:
        row = await session.get(BusinessGeminiCredential, c.a.business.id)
        swapped = SimpleNamespace(business_id=c.b.business.id, encrypted_key=row.encrypted_key)
        with pytest.raises(DomainError, match="saved Gemini key"):
            gemini.decrypt_key(c.settings, swapped)
    c.settings.integration_encryption_key = None
    response = await c.client.get(url(c.a), headers=c.a.headers)
    assert response.json()["can_update"] is False
    assert (await save(c, key=KEY_B)).status_code == 503
    c.settings.integration_encryption_key = SecretStr(Fernet.generate_key().decode())
    with pytest.raises(DomainError):
        gemini.decrypt_key(c.settings, row)


@pytest.mark.parametrize(
    "status,expected",
    [(400, 400), (401, 400), (403, 400), (404, 400), (429, 503), (500, 503), (302, 503)],
)
async def test_provider_errors_are_sanitized(monkeypatch, status, expected):
    async def get(client, endpoint, **kwargs):
        assert endpoint.startswith("https://generativelanguage.googleapis.com/v1beta/models/")
        assert KEY_A not in endpoint
        assert kwargs["headers"] == {"x-goog-api-key": KEY_A}
        return httpx.Response(status, text=f"provider response containing {KEY_A}")

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    with pytest.raises(DomainError) as failure:
        await gemini.validate_key(KEY_A, Settings(_env_file=None))
    assert failure.value.status == expected
    assert KEY_A not in str(failure.value)


async def test_validation_checks_both_default_models_without_generation(monkeypatch):
    endpoints = []

    async def get(client, endpoint, **kwargs):
        endpoints.append(endpoint)
        return httpx.Response(200, json={"name": endpoint.split("/")[-1]})

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    settings = Settings(_env_file=None)
    await gemini.validate_key(KEY_A, settings)
    assert [p.split("/")[-1] for p in endpoints] == [settings.chat_model, settings.embedding_model]


async def test_runtime_isolates_concurrent_keys_and_applies_updates(integration, monkeypatch):
    c = integration
    await save(c)
    await save(c, c.b, KEY_B)
    clients = []

    class Models:
        def __init__(self, settings):
            self.key = settings.gemini_api_key.get_secret_value()
            self.close = AsyncMock()
            clients.append(self)

    monkeypatch.setattr(runtime, "Models", Models)
    ai = runtime.BusinessAI(c.db, None, c.settings, None)

    async def use(owner):
        async with ai.models(owner.business.slug) as models:
            await asyncio.sleep(0)
            return models.key

    assert await asyncio.gather(use(c.a), use(c.b)) == [KEY_A, KEY_B]
    for client in clients:
        client.close.assert_awaited_once()
    await save(c, key=KEY_B)
    assert await use(c.a) == KEY_B
    with pytest.raises(DomainError):
        async with ai.models("unknown-business"):
            pytest.fail("No configured key must fail closed")


async def test_all_chat_and_knowledge_operations_use_the_business_key(integration, monkeypatch):
    c = integration
    await save(c)
    calls = []

    class Models:
        def __init__(self, settings):
            self.key = settings.gemini_api_key.get_secret_value()

        async def close(self):
            pass

    class Agent:
        def __init__(self, db, models, retriever, settings):
            assert models is retriever.models
            self.models = models

        async def run(self, tenant, payload, **kwargs):
            calls.append((payload.channel, self.models.key))
            return kwargs

    class Knowledge:
        def __init__(self, db, models, vectors, retriever):
            assert models is retriever.models
            self.models = models

        async def put(self, tenant, payload, **kwargs):
            calls.append(("put", self.models.key))
            return kwargs

        async def add(self, tenant, payload):
            calls.append(("add", self.models.key))

        async def update(self, tenant, payload):
            calls.append(("update", self.models.key))

    monkeypatch.setattr(runtime, "Models", Models)
    monkeypatch.setattr(runtime, "Agent", Agent)
    monkeypatch.setattr(runtime, "KnowledgeService", Knowledge)
    platform = SimpleNamespace(key="platform-key")
    ai = runtime.BusinessAI(c.db, None, c.settings, platform)
    for channel in ("admin", "instagram", "whatsapp", "telegram", "web"):
        assert await ai.run(
            c.a.business.slug, SimpleNamespace(channel=channel), capture_enquiry=False
        ) == {"capture_enquiry": False}
    assert await ai.put(c.a.business.slug, None, expected_revision="revision") == {
        "expected_revision": "revision"
    }
    await ai.add(c.a.business.slug, None)
    await ai.update(c.a.business.slug, None)
    assert len(calls) == 8 and all(key == KEY_A for _, key in calls)
    await ai.run(c.b.business.slug, SimpleNamespace(channel="web"))
    assert calls[-1] == ("web", "platform-key")
