from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from context_agent.api import create_app
from context_agent.config import Settings
from super_admin.models import SuperAdmin
from super_admin.security import AUDIENCE, ISSUER, verify_password
from super_admin.service import create_account, reset_password

PASSWORD = "A long test-only passphrase!"
SECRET = "a-test-only-signing-secret-with-at-least-32-bytes"
LOGIN = "/auth/super-admin/login"
ME = "/auth/super-admin/me"


@pytest.fixture
def settings():
    return Settings(_env_file=None, super_admin_jwt_secret=SECRET)


def make_app(db, settings):
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    return app


@pytest.fixture
async def account(db):
    return await create_account(db, "superadmin", PASSWORD)


@pytest.fixture
async def client(db, settings):
    async with AsyncClient(
        transport=ASGITransport(app=make_app(db, settings)), base_url="http://test"
    ) as client:
        yield client


async def login(client, username="superadmin", password=PASSWORD):
    return await client.post(LOGIN, json={"username": username, "password": password})


async def test_login_matches_frontend_contract_and_me_checks_token(client, account):
    response = await login(client, username=" SuperAdmin ")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"access_token", "token_type", "expires_in"}
    assert data["token_type"] == "bearer" and data["expires_in"] == 3600
    claims = jwt.decode(data["access_token"], SECRET, algorithms=["HS256"], audience=AUDIENCE)
    assert claims["sub"] == str(account.id) and claims["role"] == "super_admin"
    assert claims["exp"] - claims["iat"] == 3600
    assert response.headers["cache-control"] == "no-store"
    assert PASSWORD not in response.text
    profile = await client.get(ME, headers={"Authorization": f"Bearer {data['access_token']}"})
    assert profile.status_code == 200
    assert profile.json() == {
        "id": str(account.id),
        "username": "superadmin",
        "role": "super_admin",
    }
    assert profile.headers["cache-control"] == "no-store"


async def test_password_stored_as_salted_argon2_hash(db, account):
    second = await create_account(db, "other-admin", PASSWORD)
    assert account.password_hash.startswith("$argon2id$")
    assert account.password_hash != second.password_hash
    assert PASSWORD not in account.password_hash
    assert verify_password(PASSWORD, account.password_hash)


async def test_bad_password_unknown_and_inactive_accounts_have_same_error(client, db, account):
    wrong = await login(client, password="incorrect password")
    unknown = await login(client, username="missing-admin")
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        stored.is_active = False
    disabled = await login(client)
    for response in [wrong, unknown, disabled]:
        assert response.status_code == 401
        assert response.json() == wrong.json()
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"


async def test_failed_attempts_commit_and_lock_survives_app_restart(client, db, settings, account):
    for _ in range(settings.super_admin_login_max_attempts):
        assert (await login(client, password="incorrect password")).status_code == 401
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        assert stored.failed_login_attempts == settings.super_admin_login_max_attempts
        assert stored.locked_until is not None
    async with AsyncClient(
        transport=ASGITransport(app=make_app(db, settings)), base_url="http://test"
    ) as second_client:
        assert (await login(second_client)).status_code == 401
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        stored.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    assert (await login(client)).status_code == 200
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        assert stored.failed_login_attempts == 0 and stored.locked_until is None


async def test_success_resets_failed_attempts(client, db, account):
    assert (await login(client, password="incorrect password")).status_code == 401
    assert (await login(client)).status_code == 200
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        assert stored.failed_login_attempts == 0


@pytest.mark.parametrize("secret", [None, "", "too-short", " " * 64])
async def test_auth_fails_closed_without_signing_secret(db, secret):
    config = Settings(_env_file=None, super_admin_jwt_secret=secret)
    async with AsyncClient(
        transport=ASGITransport(app=make_app(db, config)), base_url="http://test"
    ) as client:
        response = await login(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_not_configured"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic test"},
        {"X-Super-Admin-Key": SECRET},
        {"Authorization": "Bearer invalid.token.here"},
    ],
)
async def test_me_rejects_missing_invalid_or_shared_key_credentials(client, headers):
    assert (await client.get(ME, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"exp": 1},
        {"role": "admin"},
        {"aud": "business-admin"},
        {"iss": "another-service"},
        {"sub": "not-a-uuid"},
        {"sub": str(uuid4())},
        {"ver": 999},
        {"ver": True},
        {"iat": 9999999999},
        {"exp": None},
    ],
)
async def test_me_rejects_invalid_or_revoked_claims(client, account, change):
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": str(account.id),
        "role": "super_admin",
        "ver": 1,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60,
        "jti": "test",
    }
    claims.update(change)
    if claims["exp"] is None:
        del claims["exp"]
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    assert (await client.get(ME, headers={"Authorization": f"Bearer {token}"})).status_code == 401


async def test_me_rejects_forged_signature(client, account):
    token = (await login(client)).json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(claims, "different-signing-key-at-least-32-bytes", algorithm="HS256")
    response = await client.get(ME, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_disabling_account_revokes_existing_token(client, db, account):
    token = (await login(client)).json()["access_token"]
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        stored.is_active = False
    assert (await client.get(ME, headers={"Authorization": f"Bearer {token}"})).status_code == 401


async def test_reset_password_revokes_tokens_and_clears_lock(client, db, account):
    token = (await login(client)).json()["access_token"]
    async with db.transaction() as session:
        stored = await session.get(SuperAdmin, account.id)
        stored.failed_login_attempts = 5
        stored.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    new_password = "A different long passphrase!"
    await reset_password(db, "SUPERADMIN", new_password)
    assert (await login(client)).status_code == 401
    assert (await login(client, password=new_password)).status_code == 200
    assert (await client.get(ME, headers={"Authorization": f"Bearer {token}"})).status_code == 401


async def test_account_creation_rejects_duplicate_and_weak_passwords(db, account):
    with pytest.raises(ValueError, match="already exists"):
        await create_account(db, "SUPERADMIN", PASSWORD)
    for password in ["short", " " * 20, "x" * 129]:
        with pytest.raises(ValueError, match="15 to 128"):
            await create_account(db, "other-admin", password)
    with pytest.raises(ValueError):
        await create_account(db, "bad username", PASSWORD)
    async with db.transaction() as session:
        assert len(list((await session.scalars(select(SuperAdmin))).all())) == 1


async def test_login_does_not_accept_client_selected_role(client, account):
    response = await client.post(
        LOGIN, json={"username": "superadmin", "password": PASSWORD, "role": "super_admin"}
    )
    assert response.status_code == 400
    assert PASSWORD not in response.text


async def test_expected_auth_routes_and_health_available(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert set(p for p in paths if p.startswith("/auth/")) == {LOGIN, ME, "/auth/login", "/auth/me"}
    assert (await client.post("/auth/login", json={})).status_code == 400
    assert (await client.get("/health/live")).status_code == 200
