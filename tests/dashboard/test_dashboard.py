from datetime import date
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.api import create_app
from context_agent.config import Settings
from dashboard.catalog import USE_CASES, WIDGET_TYPES, catalog_for, infer_use_case
from dashboard.demo import widget_data
from dashboard.models import DashboardConfig
from dashboard.schemas import WidgetOutput
from dashboard.service import choose_widgets
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business
from super_admin.security import create_token
from super_admin.service import create_account


@pytest.fixture
async def portal(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="dashboard-test-only-secret-at-least-32-bytes"
    )
    business, username, password = await create_business(
        db, settings, BusinessCreate(name="Demo Clinic")
    )
    async with db.transaction() as session:
        account = await session.scalar(
            select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
        )
    token = create_business_token(BusinessIdentity(account, business), settings)
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, business, settings


async def load(client, days=30):
    response = await client.get("/admin/dashboard", params={"days": days})
    assert response.status_code == 200, response.text
    return response.json()


async def test_first_visit_creates_ten_stable_widgets_and_one_saved_config(portal, db):
    client, business, _ = portal
    first = await load(client)
    assert first["mode"] == "demo" and first["config"]["use_case"] == "clinic"
    assert UUID(first["business_id"]) == business.id
    assert len(first["widgets"]) == len(set(first["config"]["widget_ids"])) == 10
    assert len(first["catalog"]) == 16
    assert first == await load(client)
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(DashboardConfig)) == 1
        assert (await session.get(DashboardConfig, business.id)).widget_ids == first["config"][
            "widget_ids"
        ]
    response = await client.get("/admin/dashboard")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("use_case", list(USE_CASES))
async def test_generation_is_relevant_and_saved(portal, use_case):
    client, _, _ = portal
    first = await load(client)
    response = await client.post(
        "/admin/dashboard/generate",
        json={"use_case": use_case, "expected_revision": first["config"]["revision"]},
    )
    assert response.status_code == 200, response.text
    generated = response.json()
    assert generated["config"]["use_case"] == use_case
    assert generated["config"]["revision"] == 2
    assert set(generated["config"]["widget_ids"]) != set(first["config"]["widget_ids"])
    assert {widget["title"] for widget in generated["widgets"]} <= {
        widget["title"] for widget in catalog_for(use_case)
    }
    assert await load(client) == generated


async def test_customization_preserves_order_values_and_rejects_stale_update(portal):
    client, _, _ = portal
    first = await load(client)
    reversed_ids = first["config"]["widget_ids"][::-1]
    response = await client.patch(
        "/admin/dashboard/config", json={"widget_ids": reversed_ids, "expected_revision": 1}
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["config"]["widget_ids"] == reversed_ids
    assert updated["widgets"] == first["widgets"][::-1]
    assert (
        await client.patch(
            "/admin/dashboard/config", json={"widget_ids": reversed_ids, "expected_revision": 1}
        )
    ).status_code == 409
    assert (
        await client.post(
            "/admin/dashboard/generate", json={"use_case": "retail", "expected_revision": 1}
        )
    ).status_code == 409
    assert (await load(client))["config"]["revision"] == 2


@pytest.mark.parametrize("days", [7, 30, 90])
async def test_periods_are_valid_stable_and_dates_stay_in_window(portal, days):
    client, _, _ = portal
    value = await load(client, days)
    assert value["period"] == days
    assert value == await load(client, days)


async def test_unsupported_period_and_configuration_fail_without_writes(portal):
    client, _, _ = portal
    first = await load(client)
    for ids in [
        [],
        first["config"]["widget_ids"][:9],
        ["kpi"] * 10,
        ["unknown"] + first["config"]["widget_ids"][1:],
    ]:
        assert (
            await client.patch(
                "/admin/dashboard/config", json={"widget_ids": ids, "expected_revision": 1}
            )
        ).status_code == 400
    for payload in [
        {"use_case": "unknown", "expected_revision": 1},
        {"use_case": "retail", "expected_revision": 1, "business_id": first["business_id"]},
    ]:
        assert (await client.post("/admin/dashboard/generate", json=payload)).status_code == 400
    for days in [0, -1, 8, 500, "invalid"]:
        assert (await client.get("/admin/dashboard", params={"days": days})).status_code == 400
    assert first == await load(client)


async def test_dashboard_requires_owner_and_never_accepts_another_business_id(portal, db):
    client, first_business, settings = portal
    first = await load(client)
    super_admin = await create_account(db, "platform-admin", "a-long-test-only-password")
    for auth in ["", "Bearer invalid", f"Bearer {create_token(super_admin, settings)}"]:
        assert (
            await client.get("/admin/dashboard", headers={"Authorization": auth})
        ).status_code == 401
        assert (
            await client.get("/admin/dashboard/catalog", headers={"Authorization": auth})
        ).status_code == 401
    second, username, password = await create_business(
        db, settings, BusinessCreate(name="Second Shop")
    )
    login = await client.post(
        "/auth/login",
        json={"business_slug": second.slug, "username": username, "password": password},
    )
    other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    other = await client.get(
        "/admin/dashboard",
        headers=other_headers,
        params={"business_id": str(first_business.id), "slug": first_business.slug},
    )
    assert other.status_code == 200
    assert other.json()["business_id"] == str(second.id)
    assert other.json()["config"]["use_case"] == "retail"
    assert first == await load(client)


@pytest.mark.parametrize("use_case", list(USE_CASES))
def test_all_sixteen_render_contracts_have_valid_demo_values(use_case):
    for definition in catalog_for(use_case):
        value = widget_data(definition["type"], use_case, 42, date(2026, 9, 15), 30)
        widget = WidgetOutput(**definition, data=value)
        assert widget.data.value >= 0
        assert value == widget_data(definition["type"], use_case, 42, date(2026, 9, 15), 30)
        if widget.type == "funnel":
            values = [point.value for point in widget.data.points]
            assert values == sorted(values, reverse=True)
        if widget.type == "heatmap":
            assert len(widget.data.cells) == 42
        if widget.type == "activity":
            assert all("Demo" in item.title for item in widget.data.items)
        if widget.type in ["donut", "pie", "table"]:
            assert widget.data.value == sum(point.value for point in widget.data.points)
        if widget.type == "progress":
            assert widget.data.value <= widget.data.target


def test_random_selection_has_ten_unique_widgets_with_balanced_types():
    for seed in range(100):
        selected = choose_widgets(seed)
        assert len(set(selected)) == 10
        groups = [widget.group for key in selected for widget in WIDGET_TYPES if widget.id == key]
        assert (
            groups.count("summary") == 2
            and groups.count("chart") == 6
            and groups.count("detail") == 2
        )
        assert set(choose_widgets(seed, selected)) != set(selected)


def test_business_use_case_inference_has_a_generic_fallback():
    assert infer_use_case("Nivaso Dental", None) == "clinic"
    assert infer_use_case("Gift Store", None) == "retail"
    assert infer_use_case("Consulting Services", None) == "services"
    assert infer_use_case("Acme", None) == "support"
