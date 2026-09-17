from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import TenantSettings
from super_admin.businesses.models import Business, BusinessAdmin
from super_admin.security import verify_password
from super_admin.service import create_account

SECRET = "business-test-only-signing-secret-at-least-32-bytes"
MANAGE = "/super-admin/businesses"


@pytest.fixture
async def client(db):
    services = {"db": db, "settings": Settings(_env_file=None, super_admin_jwt_secret=SECRET)}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def platform(client, db):
    password = "Test-only platform password"
    await create_account(db, "platform-admin", password)
    token = (
        await client.post(
            "/auth/super-admin/login", json={"username": "platform-admin", "password": password}
        )
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def create(client, platform, **values):
    response = await client.post(
        MANAGE, headers=platform, json={"name": "Bright Smile Dental", **values}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(client, business, **changes):
    return await client.post(
        "/auth/login",
        json={
            "business_slug": business["slug"],
            "username": business["admin_username"],
            "password": business["admin_password"],
            **changes,
        },
    )


async def owner_headers(client, business):
    response = await login(client, business)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_create_saved_record_and_initial_login(client, platform, db):
    business = await create(client, platform, description=" Dental care in Chennai ", plan="pro")
    assert UUID(business["_id"]).version == 4
    assert business["slug"] == business["admin_username"] == "bright-smile-dental"
    assert business["status"] == "active" and business["timezone"] == "Asia/Kolkata"
    assert business["description"] == "Dental care in Chennai" and business["plan"] == "pro"
    assert business["created_at"] and business["updated_at"]
    async with db.transaction() as session:
        account = await session.scalar(select(BusinessAdmin))
        assert account.business_id == UUID(business["_id"])
        assert account.password_hash.startswith("$argon2id$")
        assert verify_password(business["admin_password"], account.password_hash)
    owner = await owner_headers(client, business)
    for path, headers in [
        (MANAGE + "/" + business["slug"], platform),
        ("/admin/businesses/" + business["slug"], owner),
    ]:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["_id"] == business["_id"]
        assert response.json()["description"] == business["description"]
        assert "admin_password" not in response.text and "password_hash" not in response.text
    me = (await client.get("/auth/me", headers=owner)).json()
    assert me["business"]["_id"] == business["_id"] and me["role"] == "admin"
    listing = (await client.get(MANAGE, headers=platform)).json()
    assert [b["_id"] for b in listing] == [business["_id"]]
    assert business["admin_password"] not in str(listing)


async def test_branding_exposes_only_public_identity(client, platform):
    business = await create(client, platform, description="Private saved description")
    response = await client.get("/web/businesses/" + business["slug"])
    assert response.status_code == 200
    assert response.json() == {key: business[key] for key in ["_id", "slug", "name"]}
    assert (await client.get("/web/businesses/missing-business")).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"slug": "ab"},
        {"slug": "UPPERCASE"},
        {"slug": "a.b"},
        {"slug": "-clinic"},
        {"slug": "clinic-"},
        {"slug": "some clinic"},
        {"slug": "admin"},
        {"slug": "general"},
        {"slug": "x" * 64},
        {"timezone": "No/SuchZone"},
        {"name": "  "},
        {"plan": "made-up"},
        {"_id": str(uuid4())},
        {"status": "active"},
        {"admin_password": "chosen"},
    ],
)
async def test_validation_rejects_invalid_or_client_controlled_fields(
    client, platform, db, payload
):
    response = await client.post(MANAGE, headers=platform, json={"name": "Clinic", **payload})
    assert response.status_code == 400
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Business)) == 0
        assert await session.scalar(select(func.count()).select_from(BusinessAdmin)) == 0


async def test_unique_editable_slugs_and_collisions(client, platform, db):
    first = await create(client, platform)
    suggestion = await client.get(
        MANAGE + "/suggest-slug", headers=platform, params={"name": first["name"]}
    )
    assert suggestion.json() == {"slug": "bright-smile-dental-2"}
    second = await create(client, platform)
    assert second["slug"] == "bright-smile-dental-2" and second["_id"] != first["_id"]
    custom = await create(client, platform, slug="my-custom-clinic")
    assert custom["slug"] == "my-custom-clinic"
    duplicate = await client.post(
        MANAGE, headers=platform, json={"name": "Another", "slug": first["slug"]}
    )
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "slug_taken"
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Business)) == 3
        assert await session.scalar(select(func.count()).select_from(BusinessAdmin)) == 3


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Café & Dental", "cafe-dental"),
        ("你好", "business"),
        ("A", "a-business"),
        ("Admin", "admin-business"),
        ("x" * 80, "x" * 63),
    ],
)
async def test_name_suggestions_are_dns_safe(client, platform, name, expected):
    response = await client.get(MANAGE + "/suggest-slug", headers=platform, params={"name": name})
    assert response.json() == {"slug": expected}


async def test_cannot_claim_legacy_tenant_data(client, platform, db):
    async with db.transaction() as session:
        session.add(TenantSettings(tenant_id="old-clinic", instructions="Private existing tenant"))
    suggestion = await client.get(
        MANAGE + "/suggest-slug", headers=platform, params={"name": "Old Clinic"}
    )
    assert suggestion.json() == {"slug": "old-clinic-2"}
    response = await client.post(
        MANAGE, headers=platform, json={"name": "Old Clinic", "slug": "old-clinic"}
    )
    assert response.status_code == 409


async def test_roles_and_tenant_boundaries(client, platform):
    first = await create(client, platform, slug="first-clinic")
    second = await create(client, platform, slug="second-clinic")
    owner = await owner_headers(client, first)
    assert (await login(client, first, business_slug=second["slug"])).status_code == 401
    assert (await client.get("/admin/businesses/second-clinic", headers=owner)).status_code == 403
    assert [b["slug"] for b in (await client.get("/admin/businesses", headers=owner)).json()] == [
        "first-clinic"
    ]
    for headers in [{}, {"X-Internal-Key": SECRET}, platform]:
        assert (
            await client.get("/admin/businesses/first-clinic", headers=headers)
        ).status_code == 401
    for headers in [{}, {"X-Super-Admin-Key": SECRET}, owner]:
        assert (await client.get(MANAGE, headers=headers)).status_code == 401
        assert (
            await client.post(MANAGE, headers=headers, json={"name": "Forbidden Clinic"})
        ).status_code == 401
    assert (await client.get("/auth/super-admin/me", headers=owner)).status_code == 401


async def test_plan_status_persist_and_disable_access(client, platform):
    business = await create(client, platform)
    owner = await owner_headers(client, business)
    path = MANAGE + "/" + business["slug"]
    response = await client.patch(path + "/plan", headers=platform, json={"plan": "enterprise"})
    assert response.status_code == 200 and response.json()["plan"] == "enterprise"
    assert (await client.get("/auth/me", headers=owner)).json()["business"]["plan"] == "enterprise"
    for status in ["suspended", "inactive"]:
        assert (
            await client.patch(path + "/status", headers=platform, json={"status": status})
        ).status_code == 200
        assert (await login(client, business)).status_code == 401
        assert (await client.get("/admin/businesses", headers=owner)).status_code == 401
        assert (await client.get("/web/businesses/" + business["slug"])).status_code == 404
    await client.patch(path + "/status", headers=platform, json={"status": "active"})
    assert (await login(client, business)).status_code == 200
    assert (await client.get("/auth/me", headers=owner)).status_code == 401
    assert (await client.get(path, headers=platform)).json()["_id"] == business["_id"]


async def test_login_failures_persist_lockout_and_recover(client, platform, db):
    business = await create(client, platform)
    for _ in range(5):
        assert (await login(client, business, password="wrong")).status_code == 401
    assert (await login(client, business)).status_code == 401
    async with db.transaction() as session:
        account = await session.scalar(select(BusinessAdmin))
        assert account.failed_login_attempts == 5 and account.locked_until
        account.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    assert (await login(client, business)).status_code == 200
    async with db.transaction() as session:
        account = await session.scalar(select(BusinessAdmin))
        assert account.failed_login_attempts == 0 and account.locked_until is None


@pytest.mark.parametrize(
    "change",
    [
        {"business_id": str(uuid4())},
        {"business_slug": "different-clinic"},
        {"ver": 999},
        {"role": "super_admin"},
        {"aud": "super-admin"},
        {"exp": 1},
        {"business_id": None},
    ],
)
async def test_rejects_wrong_business_claims(client, platform, change):
    business = await create(client, platform)
    response = await login(client, business)
    claims = jwt.decode(response.json()["access_token"], options={"verify_signature": False})
    claims.update(change)
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401


async def test_disabled_admin_loses_access(client, platform, db):
    business = await create(client, platform)
    owner = await owner_headers(client, business)
    async with db.transaction() as session:
        account = await session.scalar(select(BusinessAdmin))
        account.is_active = False
    assert (await client.get("/auth/me", headers=owner)).status_code == 401
    assert (await login(client, business)).status_code == 401


async def test_owner_password_recovery_revokes_old_tokens(client, platform, db):
    from super_admin.businesses.service import reset_business_password

    business = await create(client, platform)
    owner = await owner_headers(client, business)
    new_password = "A new business-only test password"
    await reset_business_password(db, business["admin_username"], new_password)
    assert (await login(client, business)).status_code == 401
    assert (await login(client, business, password=new_password)).status_code == 200
    assert (await client.get("/auth/me", headers=owner)).status_code == 401


async def test_failed_admin_insert_rolls_back_business(client, platform, db):
    first = await create(client, platform)
    async with db.transaction() as session:
        account = await session.scalar(select(BusinessAdmin))
        account.username = "conflicting-admin"
    response = await client.post(
        MANAGE, headers=platform, json={"name": "New Clinic", "slug": "conflicting-admin"}
    )
    assert response.status_code == 409
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Business)) == 1
        assert await session.scalar(select(func.count()).select_from(BusinessAdmin)) == 1
        assert (await session.scalar(select(Business))).slug == first["slug"]
