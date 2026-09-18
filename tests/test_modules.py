from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from context_agent import support
from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.db import SupportTicket
from context_agent.tools import SUPPORT, ToolContext, available_tools, execute
from modules.models import BusinessModules
from modules.service import DEFAULT_SELECTION
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business
from super_admin.security import create_token
from super_admin.service import create_account


@pytest.fixture
async def modules(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="modules-tests-signing-secret-32-bytes"
    )
    account = await create_account(db, "module-admin", "test-only-password-123")
    platform = {"Authorization": f"Bearer {create_token(account, settings)}"}
    owners = []
    for name in ("Module Test A", "Module Test B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            admin = await session.scalar(
                select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
            )
        token = create_business_token(BusinessIdentity(admin, business), settings)
        owners.append(
            SimpleNamespace(business=business, headers={"Authorization": f"Bearer {token}"})
        )
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            db=db,
            settings=settings,
            account=account,
            platform=platform,
            a=owners[0],
            b=owners[1],
        )


def management(owner):
    return f"/super-admin/businesses/{owner.business.slug}/modules"


async def read(m, owner=None):
    response = await m.client.get(management(owner or m.a), headers=m.platform)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def save(m, **changes):
    current = await read(m)
    response = await m.client.put(
        management(m.a),
        headers=m.platform,
        json={
            "selection": {**current["selection"], **changes},
            "expected_revision": current["revision"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_new_business_defaults_and_owner_entitlements(modules):
    m = modules
    current = await read(m)
    assert current["selection"] == DEFAULT_SELECTION and current["revision"] == 1
    async with m.db.transaction() as session:
        assert await session.get(BusinessModules, m.a.business.id) is not None
    url = f"/admin/businesses/{m.a.business.slug}/entitlements"
    response = await m.client.get(url, headers=m.a.headers)
    assert response.status_code == 200
    flags = response.json()["flags"]
    assert flags["support.tickets_enabled"] and not flags["module.customers"]
    assert not flags["module.products"] and not flags["orders.enabled"]
    assert not flags["module.custom_fields"]
    assert response.json()["business_id"] == str(m.a.business.id)
    assert (await m.client.get(url, headers=m.b.headers)).status_code == 403
    for headers in ({}, m.platform):
        assert (await m.client.get(url, headers=headers)).status_code == 401


async def test_only_super_admin_can_change_settings_and_stale_saves_fail(modules):
    m = modules
    current = await read(m)
    body = {"selection": current["selection"], "expected_revision": current["revision"]}
    for headers in ({}, m.a.headers, m.b.headers, {"Authorization": "Bearer invalid"}):
        assert (await m.client.get(management(m.a), headers=headers)).status_code == 401
        assert (await m.client.put(management(m.a), headers=headers, json=body)).status_code == 401
    result = await save(m, leads=True)
    assert result["revision"] == 2 and result["updated_by"] == str(m.account.id)
    assert (await m.client.put(management(m.a), headers=m.platform, json=body)).status_code == 409
    assert await read(m) == result
    assert (await read(m, m.b))["selection"] == DEFAULT_SELECTION
    assert (
        await m.client.get("/super-admin/businesses/missing/modules", headers=m.platform)
    ).status_code == 404


@pytest.mark.parametrize("pair", ["product_orders", "service_appointments"])
async def test_pair_and_customer_dependencies(modules, pair):
    m = modules
    current = await read(m)
    response = await m.client.put(
        management(m.a),
        headers=m.platform,
        json={
            "selection": {**current["selection"], pair: True},
            "expected_revision": current["revision"],
        },
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "module_dependency"
    result = await save(m, **{pair: True, "customers": True})
    body = {
        "selection": {**result["selection"], "customers": False},
        "expected_revision": result["revision"],
    }
    assert (await m.client.put(management(m.a), headers=m.platform, json=body)).status_code == 400
    assert await read(m) == result
    flags = (
        await m.client.get(
            f"/admin/businesses/{m.a.business.slug}/entitlements", headers=m.a.headers
        )
    ).json()["flags"]
    assert flags["module.products"] == flags["orders.enabled"] == (pair == "product_orders")
    assert (
        flags["module.services"] == flags["module.appointments"] == (pair == "service_appointments")
    )
    assert flags["module.customers"] and flags["module.custom_fields"]
    await save(m, **{pair: False, "customers": False})


async def test_invalid_selection_cannot_set_individual_flags_or_ownership(modules):
    m = modules
    for selection in [
        {**DEFAULT_SELECTION, "products": True},
        {**DEFAULT_SELECTION, "product_orders": "true"},
        {**DEFAULT_SELECTION, "customers": None},
        {"support_tickets": True},
    ]:
        assert (
            await m.client.put(
                management(m.a),
                headers=m.platform,
                json={
                    "selection": selection,
                    "expected_revision": 1,
                },
            )
        ).status_code == 400
    for changes in [
        {"business_id": str(m.b.business.id)},
        {"expected_revision": -1},
        {"expected_revision": True},
    ]:
        assert (
            await m.client.put(
                management(m.a),
                headers=m.platform,
                json={
                    "selection": DEFAULT_SELECTION,
                    "expected_revision": 1,
                    **changes,
                },
            )
        ).status_code == 400


async def test_disabling_catalog_blocks_every_api_and_keeps_data(modules):
    m = modules
    base = f"/admin/{m.a.business.slug}"
    await save(m, product_orders=True, customers=True)
    f = await m.client.post(
        base + "/custom-fields/product",
        headers=m.a.headers,
        json={
            "key": "color",
            "label": "Color",
            "field_type": "text",
        },
    )
    p = await m.client.post(
        base + "/products",
        headers=m.a.headers,
        json={
            "name": "Saved product",
            "price": "10.50",
            "attributes": {"color": "Blue"},
        },
    )
    assert f.status_code == p.status_code == 201
    await save(m, product_orders=False, customers=False)
    requests = [
        ("GET", "/products", None),
        ("POST", "/products", {"name": "x", "price": "1"}),
        ("GET", "/products/" + p.json()["id"], None),
        ("PATCH", "/products/" + p.json()["id"], {"name": "changed"}),
        ("DELETE", "/products/" + p.json()["id"], None),
        ("GET", "/custom-fields/product", None),
        ("POST", "/custom-fields/product", {"key": "x", "label": "X", "field_type": "text"}),
        ("PATCH", "/custom-fields/product/" + f.json()["id"], {"label": "Changed"}),
        ("DELETE", "/custom-fields/product/" + f.json()["id"], None),
    ]
    for method, url, body in requests:
        result = await m.client.request(method, base + url, headers=m.a.headers, json=body)
        assert result.status_code == 403, result.text
        assert result.json()["error"]["code"] == "module_disabled"
    await save(m, product_orders=True, customers=True)
    assert (
        await m.client.get(base + "/products/" + p.json()["id"], headers=m.a.headers)
    ).json() == p.json()
    assert (await m.client.get(base + "/custom-fields/product", headers=m.a.headers)).json() == [
        f.json()
    ]


async def test_service_fields_have_separate_access(modules):
    m = modules
    await save(m, service_appointments=True, customers=True)
    base = f"/admin/{m.a.business.slug}/custom-fields"
    assert (await m.client.get(base + "/service", headers=m.a.headers)).status_code == 200
    assert (await m.client.get(base + "/product", headers=m.a.headers)).status_code == 403


@pytest.mark.parametrize("prefix", ["/api/v1/tenants", "/admin"])
async def test_support_without_customers_and_disabling_stops_creation_and_access(modules, prefix):
    m = modules
    tenant = m.a.business.slug
    async with m.db.transaction(tenant) as session:
        ticket = await support.create_or_get(
            session, tenant, uuid4(), "Visitor question", "Needs help"
        )
        ref = ticket.ticket_ref
        assert SUPPORT.name in await available_tools(session, tenant, [])
    base = f"{prefix}/{tenant}/support-tickets"
    assert (await m.client.get(base, headers=m.a.headers)).json()["tickets"][0]["ticket_ref"] == ref
    for method, url, body in [
        ("GET", base, None),
        ("GET", base + "/" + ref, None),
        ("PATCH", base + "/" + ref, {"status": "resolved"}),
    ]:
        assert (await m.client.request(method, url, json=body)).status_code == 401
        assert (
            await m.client.request(method, url, headers=m.b.headers, json=body)
        ).status_code == 403
    await save(m, support_tickets=False)
    for method, url, body in [
        ("GET", base, None),
        ("GET", base + "/" + ref, None),
        ("PATCH", base + "/" + ref, {"status": "resolved"}),
    ]:
        assert (
            await m.client.request(method, url, headers=m.a.headers, json=body)
        ).status_code == 403
    async with m.db.transaction(tenant) as session:
        assert SUPPORT.name not in await available_tools(session, tenant, [])
    result = await execute(
        SUPPORT,
        {"question": "Another question", "reason": "Needs help"},
        ToolContext(tenant, uuid4(), "test", m.settings, db=m.db),
    )
    assert result["ok"] is False
    async with m.db.transaction() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(SupportTicket).where(SupportTicket.tenant_id == tenant)
                    )
                ).all()
            )
            == 1
        )
    await save(m, support_tickets=True)
    assert (await m.client.get(base + "/" + ref, headers=m.a.headers)).json()["status"] == "open"


async def test_leads_and_customers_can_be_enabled_independently(modules):
    m = modules
    result = await save(m, leads=True, customers=False, support_tickets=False)
    assert result["selection"] == {**DEFAULT_SELECTION, "leads": True, "support_tickets": False}
    await save(m, leads=False, customers=True)
    flags = (
        await m.client.get(
            f"/admin/businesses/{m.a.business.slug}/entitlements", headers=m.a.headers
        )
    ).json()["flags"]
    assert flags["module.customers"] and not flags["module.leads"]
    assert not flags["module.products"] and not flags["module.services"]
