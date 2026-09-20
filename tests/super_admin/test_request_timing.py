from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_ai_usage import record

from context_agent.api import create_app
from context_agent.config import Settings
from super_admin.businesses.models import Business
from super_admin.service import create_account

PASSWORD = "A long test-only passphrase!"
SECRET = "a-test-only-signing-secret-with-at-least-32-bytes"
LOGIN = "/auth/super-admin/login"
ENDPOINT = "/super-admin/request-timing"


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


@pytest.fixture
async def headers(client, account):
    response = await client.post(LOGIN, json={"username": "superadmin", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def seed(db):
    async with db.transaction() as session:
        session.add(Business(id=uuid4(), slug="acme", name="Acme Clinic"))
        session.add_all(
            [
                record(
                    "acme",
                    datetime(2026, 9, 19, 10, tzinfo=UTC),
                    queue_ms=50.0,
                    db_ms=120.0,
                    ai_ms=800.0,
                    tool_ms=0.0,
                    send_ms=200.0,
                ),
                record(
                    "acme",
                    datetime(2026, 9, 19, 11, tzinfo=UTC),
                    "whatsapp",
                    status="send_failed",
                    duration_ms=32000,
                ),
                # No matching Business row; should still show up, keyed by slug.
                record("unknown-tenant", datetime(2026, 9, 19, 12, tzinfo=UTC)),
            ]
        )


async def test_requires_auth_and_lists_across_tenants(client, headers, db):
    await seed(db)
    assert (await client.get(ENDPOINT)).status_code == 401
    response = await client.get(
        ENDPOINT, params={"start": "2026-09-19", "end": "2026-09-19"}, headers=headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_count"] == 3
    assert data["summary"]["failed_replies"] == 1
    slugs = [item["business_slug"] for item in data["items"]]
    assert slugs == ["unknown-tenant", "acme", "acme"]
    names = {item["business_slug"]: item["business_name"] for item in data["items"]}
    assert names["acme"] == "Acme Clinic"
    assert names["unknown-tenant"] == "unknown-tenant"


async def test_filters_by_business_channel_and_status(client, headers, db):
    await seed(db)
    params = {"start": "2026-09-19", "end": "2026-09-19", "business": "acme"}
    response = await client.get(ENDPOINT, params=params, headers=headers)
    data = response.json()
    assert data["total_count"] == 2
    filtered = await client.get(
        ENDPOINT, params={**params, "channel": "whatsapp"}, headers=headers
    )
    assert filtered.json()["total_count"] == 1
    by_status = await client.get(
        ENDPOINT, params={**params, "status": "send_failed"}, headers=headers
    )
    assert by_status.json()["total_count"] == 1
    assert by_status.json()["items"][0]["duration_ms"] == 32000


async def test_phase_breakdown_and_slowest_summary(client, headers, db):
    await seed(db)
    response = await client.get(
        ENDPOINT, params={"start": "2026-09-19", "end": "2026-09-19", "business": "acme"},
        headers=headers,
    )
    data = response.json()
    with_breakdown = next(item for item in data["items"] if item["queue_ms"] is not None)
    assert with_breakdown["db_ms"] == 120.0
    assert with_breakdown["ai_ms"] == 800.0
    assert with_breakdown["send_ms"] == 200.0
    assert data["summary"]["slowest_duration_ms"] == 32000


async def test_invalid_date_range_rejected(client, headers):
    response = await client.get(
        ENDPOINT, params={"start": "2026-09-19", "end": "2026-09-18"}, headers=headers
    )
    assert response.status_code == 400
