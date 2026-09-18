from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from super_admin import temporary_setup
from super_admin.models import SuperAdmin
from super_admin.security import verify_password
from super_admin.service import create_account


async def test_provisions_only_once(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert await temporary_setup.provision_once(db) == "created"
    assert await temporary_setup.provision_once(db) == "exists"
    async with db.transaction() as session:
        accounts = list(await session.scalars(select(SuperAdmin)))
    assert len(accounts) == 1
    assert accounts[0].username == "superadmin"
    assert accounts[0].is_active


async def test_existing_password_and_sessions_are_unchanged(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    password = "existing-account-test-only-password"
    existing = await create_account(db, "superadmin", password)
    assert await temporary_setup.provision_once(db) == "exists"
    async with db.transaction() as session:
        account = await session.get(SuperAdmin, existing.id)
    assert verify_password(password, account.password_hash)
    assert account.token_version == existing.token_version


async def test_preview_cannot_provision(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert await temporary_setup.provision_once(db) == "skipped"
    async with db.transaction() as session:
        assert await session.scalar(select(SuperAdmin)) is None


async def test_expired_setup_cannot_provision(db, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(temporary_setup, "EXPIRES_AT", datetime.now(UTC) - timedelta(seconds=1))
    assert await temporary_setup.provision_once(db) == "skipped"
    async with db.transaction() as session:
        assert await session.scalar(select(SuperAdmin)) is None
