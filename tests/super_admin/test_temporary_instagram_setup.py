import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import ChannelAccount
from super_admin import temporary_instagram_setup as setup
from super_admin.businesses.models import Business
from super_admin.models import SuperAdmin
from super_admin.security import create_token

SECRET = "0" * 32
PATH = "/super-admin/setup/instagram-app-secret"


@pytest.fixture
async def configured(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(setup, "EXPIRES_AT", datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(setup, "SECRET_SHA256", hashlib.sha256(SECRET.encode()).hexdigest())
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="test-only-jwt-secret-over-32-characters"
    )
    async with db.transaction() as session:
        session.add(Business(id=setup.BUSINESS_ID, slug=setup.BUSINESS_SLUG, name="Test business"))
        admin = SuperAdmin(username="testadmin", password_hash="not-used")
        session.add(admin)
        session.add(
            ChannelAccount(
                tenant_id=setup.BUSINESS_SLUG,
                channel="instagram",
                account_id=setup.ACCOUNT_ID,
                config={
                    "account_id": setup.ACCOUNT_ID,
                    "verify_token": "keep-test-verify-token",
                    "access_token": "keep-test-access-token",
                },
            )
        )
        await session.flush()
    services = {"db": db, "settings": settings, "agent": None}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, {"Authorization": "Bearer " + create_token(admin, settings)}


async def test_requires_auth_and_expected_secret(db, configured):
    client, headers = configured
    assert (await client.put(PATH, json={"app_secret": SECRET})).status_code == 401
    assert (
        await client.put(PATH, headers=headers, json={"app_secret": "1" * 32})
    ).status_code == 400
    async with db.transaction() as session:
        account = await session.scalar(select(ChannelAccount))
        assert "app_secret" not in account.config


async def test_save_is_idempotent_preserves_credentials_and_verifies_signatures(db, configured):
    client, headers = configured
    for _ in range(2):
        response = await client.put(PATH, headers=headers, json={"app_secret": SECRET})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert SECRET not in response.text
    async with db.transaction() as session:
        account = await session.scalar(select(ChannelAccount))
        assert account.config == {
            "account_id": setup.ACCOUNT_ID,
            "verify_token": "keep-test-verify-token",
            "access_token": "keep-test-access-token",
            "app_secret": SECRET,
        }
    raw = json.dumps(
        {"object": "instagram", "entry": [{"id": setup.ACCOUNT_ID, "messaging": []}]}
    ).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    signed = await client.post(
        "/webhooks/instagram", content=raw, headers={"X-Hub-Signature-256": signature}
    )
    assert signed.status_code == 200
    invalid = await client.post(
        "/webhooks/instagram", content=raw, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
    )
    assert invalid.status_code == 401
    verified = await client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "keep-test-verify-token",
            "hub.challenge": "12345",
        },
    )
    assert verified.status_code == 200 and verified.text == "12345"


@pytest.mark.parametrize(
    "case", ["other_owner", "inactive_business", "incomplete_account", "expired", "preview"]
)
async def test_rejects_wrong_target_or_unavailable_setup(db, configured, monkeypatch, case):
    client, headers = configured
    async with db.transaction() as session:
        account = await session.scalar(select(ChannelAccount))
        if case == "other_owner":
            account.tenant_id = "different-business"
        if case == "inactive_business":
            business = await session.get(Business, setup.BUSINESS_ID)
            business.status = "suspended"
        if case == "incomplete_account":
            account.config = {"account_id": setup.ACCOUNT_ID}
    if case == "expired":
        monkeypatch.setattr(setup, "EXPIRES_AT", datetime.now(UTC) - timedelta(seconds=1))
    if case == "preview":
        monkeypatch.setenv("VERCEL_ENV", "preview")
    response = await client.put(PATH, headers=headers, json={"app_secret": SECRET})
    assert response.status_code == (410 if case in {"expired", "preview"} else 409)
    async with db.transaction() as session:
        account = await session.scalar(select(ChannelAccount))
        assert "app_secret" not in account.config
