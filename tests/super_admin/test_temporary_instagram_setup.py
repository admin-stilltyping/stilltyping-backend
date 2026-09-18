import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import ChannelAccount
from super_admin import temporary_instagram_setup as setup
from super_admin.businesses.models import Business
from super_admin.models import SuperAdmin
from super_admin.security import create_token

TOKEN = "test-only-instagram-verify-token-123456789"
PATH = "/super-admin/setup/instagram-verification"


@pytest.fixture
async def configured(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(setup, "EXPIRES_AT", datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(setup, "TOKEN_SHA256", hashlib.sha256(TOKEN.encode()).hexdigest())
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="test-only-jwt-secret-over-32-characters"
    )
    async with db.transaction() as session:
        session.add(Business(id=setup.BUSINESS_ID, slug=setup.BUSINESS_SLUG, name="Test business"))
        admin = SuperAdmin(username="testadmin", password_hash="not-used")
        session.add(admin)
        await session.flush()
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, {"Authorization": "Bearer " + create_token(admin, settings)}


async def test_requires_auth_and_rejects_wrong_token_without_write(db, configured):
    client, headers = configured
    assert (await client.put(PATH, json={"verify_token": TOKEN})).status_code == 401
    response = await client.put(PATH, headers=headers, json={"verify_token": "x" * 40})
    assert response.status_code == 400
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(ChannelAccount)) == 0


async def test_saves_idempotently_and_passes_meta_challenge(db, configured):
    client, headers = configured
    first = await client.put(PATH, headers=headers, json={"verify_token": TOKEN})
    assert first.status_code == 200 and first.json()["created"]
    assert first.headers["cache-control"] == "no-store"
    assert TOKEN not in first.text and not first.json()["messaging_credentials_present"]
    second = await client.put(PATH, headers=headers, json={"verify_token": TOKEN})
    assert second.status_code == 200 and not second.json()["created"]
    async with db.transaction() as session:
        row = await session.scalar(select(ChannelAccount))
        assert row.config == {"account_id": setup.ACCOUNT_ID, "verify_token": TOKEN}
        assert row.tenant_id == setup.BUSINESS_SLUG
        assert await session.scalar(select(func.count()).select_from(ChannelAccount)) == 1
    verified = await client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TOKEN,
            "hub.challenge": "987654321",
        },
    )
    assert verified.status_code == 200 and verified.text == "987654321"
    invalid = await client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "987654321",
        },
    )
    assert invalid.status_code == 403


async def test_preserves_existing_credentials(db, configured):
    client, headers = configured
    async with db.transaction() as session:
        session.add(
            ChannelAccount(
                tenant_id=setup.BUSINESS_SLUG,
                channel="instagram",
                account_id=setup.ACCOUNT_ID,
                config={
                    "access_token": "keep-test-access",
                    "app_secret": "keep-test-secret",
                    "verify_token": "old",
                },
            )
        )
    response = await client.put(PATH, headers=headers, json={"verify_token": TOKEN})
    assert response.status_code == 200 and response.json()["messaging_credentials_present"]
    async with db.transaction() as session:
        row = await session.scalar(select(ChannelAccount))
        assert row.config["access_token"] == "keep-test-access"
        assert row.config["app_secret"] == "keep-test-secret"


@pytest.mark.parametrize(
    "case", ["different_owner", "other_account", "business_mismatch", "expired"]
)
async def test_rejects_unexpected_target_and_expiry(db, configured, monkeypatch, case):
    client, headers = configured
    async with db.transaction() as session:
        if case == "different_owner":
            session.add(
                ChannelAccount(
                    tenant_id="other", channel="instagram", account_id=setup.ACCOUNT_ID, config={}
                )
            )
        if case == "other_account":
            session.add(
                ChannelAccount(
                    tenant_id=setup.BUSINESS_SLUG, channel="instagram", account_id="999", config={}
                )
            )
    if case == "business_mismatch":
        monkeypatch.setattr(setup, "BUSINESS_ID", uuid4())
    if case == "expired":
        monkeypatch.setattr(setup, "EXPIRES_AT", datetime.now(UTC) - timedelta(seconds=1))
    response = await client.put(PATH, headers=headers, json={"verify_token": TOKEN})
    assert response.status_code == (410 if case == "expired" else 409)
    async with db.transaction() as session:
        rows = list(await session.scalars(select(ChannelAccount)))
        assert all(not row.config for row in rows)
