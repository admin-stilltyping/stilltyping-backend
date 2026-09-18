import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.performance import RequestTiming, current_timing
from context_agent.performance_routes import explain_select
from modules.models import BusinessModules
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin


def test_partitions_overlapping_spans_and_preserves_application_time():
    timing = RequestTiming(started=0)
    timing.spans = [
        ("db_acquire", 1, 4),
        ("db_query", 2, 3),
        ("db_lock", 5, 7),
        ("db_finish", 8, 9),
    ]
    assert timing.snapshot(10) == {
        "db_acquire": 2000,
        "db_query": 1000,
        "db_lock": 2000,
        "db_finish": 1000,
        "app": 4000,
        "total": 10000,
        "query_count": 0,
    }


@pytest.fixture
async def profile_client(db):
    async with db.transaction() as session:
        business = Business(slug="profile-clinic", name="Private test business")
        session.add(business)
        await session.flush()
        account = BusinessAdmin(
            business_id=business.id, username="owner", password_hash="never-log-this-hash"
        )
        session.add_all(
            [
                account,
                BusinessModules(business_id=business.id, service_appointments=True, customers=True),
            ]
        )
        await session.flush()
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="test-timing-only-secret-32-bytes-long"
    )
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    token = create_business_token(BusinessIdentity(account, business), settings)
    headers = {"Authorization": "Bearer " + token, "X-Request-Timing": "1"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, headers


async def test_owner_timings_count_real_queries_and_do_not_log_private_values(
    profile_client, caplog
):
    client, headers = profile_client
    caplog.set_level("INFO", logger="uvicorn.error")
    response = await client.get("/admin/profile-clinic/services", headers=headers)
    assert response.status_code == 200
    assert 'db_count;desc="5"' in response.headers["server-timing"]
    assert len(response.headers["x-timing-id"]) == 32
    assert "request_timing" in caplog.text
    assert "profile-clinic" not in caplog.text
    assert "never-log-this-hash" not in caplog.text
    assert headers["Authorization"] not in caplog.text
    metrics = json.loads(caplog.records[-1].message.split("request_timing ")[1])["milliseconds"]
    assert metrics["db_query"] > 0
    assert metrics["db_acquire"] > 0
    assert metrics["db_finish"] > 0
    assert (
        abs(
            sum(metrics[k] for k in ("db_query", "db_lock", "db_acquire", "db_finish", "app"))
            - metrics["total"]
        )
        < 0.06
    )
    assert current_timing.get() is None


async def test_opt_in_and_auth_are_required_and_diagnostics_are_tenant_scoped(profile_client):
    client, headers = profile_client
    ordinary = await client.get("/auth/me", headers={"Authorization": headers["Authorization"]})
    denied = await client.get("/auth/me", headers={"X-Request-Timing": "1"})
    assert ordinary.status_code == 200 and denied.status_code == 401
    assert "server-timing" not in ordinary.headers
    assert "server-timing" not in denied.headers
    assert (
        await client.get("/admin/another-clinic/diagnostics/database", headers=headers)
    ).status_code == 403
    assert (
        await client.get("/admin/profile-clinic/diagnostics/database", headers=headers)
    ).status_code == 400


async def test_concurrent_requests_keep_query_counts_separate(profile_client):
    client, headers = profile_client
    me, services = await asyncio.gather(
        client.get("/auth/me", headers=headers),
        client.get("/admin/profile-clinic/services", headers=headers),
    )
    assert me.status_code == services.status_code == 200
    assert 'db_count;desc="2"' in me.headers["server-timing"]
    assert 'db_count;desc="5"' in services.headers["server-timing"]
    assert me.headers["x-timing-id"] != services.headers["x-timing-id"]


async def test_query_failures_are_timed_and_transaction_is_still_rolled_back(db):
    timing = RequestTiming()
    token = current_timing.set(timing)
    try:
        with pytest.raises(SQLAlchemyError):
            async with db.transaction() as session:
                await session.execute(text("SELECT * FROM timing_table_does_not_exist"))
        assert timing.queries == 1
        assert any(kind == "db_query" for kind, _, _ in timing.spans)
        assert any(kind == "db_finish" for kind, _, _ in timing.spans)
    finally:
        current_timing.reset(token)
    async with db.transaction() as session:
        assert await session.scalar(select(1)) == 1


async def test_explain_returns_only_timing_values_not_query_plans(db):
    class Session:
        async def scalar(self, statement):
            assert str(statement).startswith("EXPLAIN (ANALYZE, FORMAT JSON) SELECT")
            return [
                {
                    "Planning Time": 0.002,
                    "Execution Time": 0.003,
                    "Plan": {"Secret": "must not be returned"},
                }
            ]

    result = await explain_select(Session(), db.engine.dialect, "baseline", select(1))
    assert result["postgres_planning_ms"] == 0.002
    assert result["postgres_execution_ms"] == 0.003
    assert "Secret" not in json.dumps(result)
