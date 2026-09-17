from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from context_agent.api import create_app
from context_agent.config import Settings
from custom_fields.models import FieldDefinition
from modules.models import BusinessModules
from products.models import Product
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin
from super_admin.security import create_token
from super_admin.service import create_account


@pytest.fixture
async def catalog(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="catalog-test-only-secret-32-bytes-long"
    )
    owners = []
    async with db.transaction() as session:
        for slug in ("clothing", "electronics"):
            business = Business(slug=slug, name=slug, timezone="Asia/Kolkata")
            session.add(business)
            await session.flush()
            session.add(
                BusinessModules(
                    business_id=business.id,
                    product_orders=True,
                    service_appointments=True,
                    customers=True,
                )
            )
            account = BusinessAdmin(business_id=business.id, username=slug, password_hash="unused")
            session.add(account)
            await session.flush()
            token = create_business_token(BusinessIdentity(account, business), settings)
            owners.append(
                SimpleNamespace(
                    business=business,
                    headers={"Authorization": f"Bearer {token}"},
                    products=f"/admin/{slug}/products",
                    fields=f"/admin/{slug}/custom-fields/product",
                )
            )
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, a=owners[0], b=owners[1], db=db, settings=settings)


async def field(catalog, owner=None, **values):
    owner = owner or catalog.a
    response = await catalog.client.post(
        owner.fields,
        headers=owner.headers,
        json={
            "key": "color",
            "label": "Color",
            "field_type": "text",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def product(catalog, owner=None, **values):
    owner = owner or catalog.a
    response = await catalog.client.post(
        owner.products,
        headers=owner.headers,
        json={
            "name": "Cotton Shirt",
            "price": "799.10",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_each_business_uses_its_own_definitions_and_products(catalog):
    c = catalog
    await field(c, required=True)
    await field(c, c.b, field_type="number", required=True)
    a = await product(c, attributes={"color": "Blue"}, sku="SAME")
    b = await product(c, c.b, attributes={"color": 16}, sku="SAME")
    assert a["business_id"] == str(c.a.business.id)
    assert UUID(a["id"]).version == 4
    for owner, item in [(c.a, a), (c.b, b)]:
        response = await c.client.get(owner.products, headers=owner.headers)
        assert response.json()["items"] == [item]
        assert response.json()["total"] == 1
        assert (
            await c.client.get(owner.products + "/" + item["id"], headers=owner.headers)
        ).json() == item
    async with c.db.transaction() as session:
        saved = await session.get(Product, UUID(a["id"]))
        assert saved.price == Decimal("799.10")
        assert saved.attributes == {"color": "Blue"}
        assert saved.business_id == c.a.business.id


async def test_all_endpoints_require_owner_and_scope_record_ids(catalog):
    c = catalog
    f = await field(c)
    p = await product(c)
    platform = await create_account(c.db, "platform", "a-test-password-at-least-12")
    platform_headers = {"Authorization": f"Bearer {create_token(platform, c.settings)}"}
    requests = [
        ("GET", c.a.products, None),
        ("POST", c.a.products, {"name": "x", "price": "1"}),
        ("GET", c.a.products + "/" + p["id"], None),
        ("PATCH", c.a.products + "/" + p["id"], {"name": "changed"}),
        ("DELETE", c.a.products + "/" + p["id"], None),
        ("GET", c.a.fields, None),
        ("POST", c.a.fields, {"key": "x", "label": "X", "field_type": "text"}),
        ("PATCH", c.a.fields + "/" + f["id"], {"label": "changed"}),
        ("DELETE", c.a.fields + "/" + f["id"], None),
    ]
    for method, url, body in requests:
        for headers in ({}, {"Authorization": "Bearer invalid"}, platform_headers):
            assert (
                await c.client.request(method, url, headers=headers, json=body)
            ).status_code == 401
        assert (
            await c.client.request(method, url, headers=c.b.headers, json=body)
        ).status_code == 403
        if url.endswith((p["id"], f["id"])):
            own_url = url.replace("/clothing/", "/electronics/")
            assert (
                await c.client.request(method, own_url, headers=c.b.headers, json=body)
            ).status_code == 404
    for payload in ({"business_id": str(c.b.business.id)}, {"tenant_id": "electronics"}):
        assert (
            await c.client.post(
                c.a.products,
                headers=c.a.headers,
                json={
                    "name": "Injected",
                    "price": "1",
                    **payload,
                },
            )
        ).status_code == 400
    async with c.db.transaction() as session:
        business = await session.get(Business, c.a.business.id)
        business.status = "suspended"
    assert (await c.client.get(c.a.products, headers=c.a.headers)).status_code == 401


@pytest.mark.parametrize(
    "kind,value,invalid",
    [
        ("text", "Cotton", 12),
        ("number", 0, "12"),
        ("boolean", False, "false"),
        ("date", "2028-02-29", "2026-02-29"),
        ("select", "blue", "green"),
        ("multiselect", ["blue", "red"], ["blue", "blue"]),
    ],
)
async def test_custom_types_and_required_values(catalog, kind, value, invalid):
    c = catalog
    options = [{"value": key, "label": key.title()} for key in ["blue", "red"]]
    await field(
        c,
        field_type=kind,
        required=True,
        **({"options": options} if kind in {"select", "multiselect"} else {}),
    )
    p = await product(c, attributes={"color": value})
    assert p["attributes"] == {"color": value}
    for attributes in [{}, {"color": None}, {"color": invalid}, {"other": value}]:
        response = await c.client.post(
            c.a.products,
            headers=c.a.headers,
            json={
                "name": "Invalid",
                "price": "1",
                "attributes": attributes,
            },
        )
        assert response.status_code == 400, response.text
    response = await c.client.patch(
        c.a.products + "/" + p["id"], headers=c.a.headers, json={"attributes": {"color": invalid}}
    )
    assert response.status_code == 400
    saved = (await c.client.get(c.a.products + "/" + p["id"], headers=c.a.headers)).json()
    assert saved["attributes"] == {"color": value}


async def test_partial_updates_clear_optional_values_and_preserve_others(catalog):
    c = catalog
    await field(c)
    await field(c, key="weight", label="Weight", field_type="number")
    p = await product(c, sku="SKU-1", category="Clothes", attributes={"color": "Blue", "weight": 0})
    url = c.a.products + "/" + p["id"]
    response = await c.client.patch(url, headers=c.a.headers, json={"name": "Updated"})
    assert response.json()["attributes"] == p["attributes"]
    response = await c.client.patch(
        url,
        headers=c.a.headers,
        json={
            "attributes": {"color": None},
            "sku": "",
            "category": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"weight": 0}
    assert response.json()["sku"] is None and response.json()["category"] is None
    for value in [True, 10**500, {"nested": 1}, [1]]:
        response = await c.client.patch(
            url, headers=c.a.headers, json={"attributes": {"weight": value}}
        )
        assert response.status_code == 400


async def test_schema_edits_archive_and_retained_values(catalog):
    c = catalog
    f = await field(c, required=True)
    p = await product(c, attributes={"color": "Blue"})
    field_url, product_url = c.a.fields + "/" + f["id"], c.a.products + "/" + p["id"]
    assert (
        await c.client.patch(field_url, headers=c.a.headers, json={"label": "New label"})
    ).status_code == 200
    assert (
        await c.client.patch(field_url, headers=c.a.headers, json={"key": "new_key"})
    ).status_code == 400
    assert (
        await c.client.patch(field_url, headers=c.a.headers, json={"field_type": "number"})
    ).status_code == 409
    assert (await c.client.delete(field_url, headers=c.a.headers)).status_code == 204
    assert (await c.client.get(c.a.fields, headers=c.a.headers)).json() == []
    archived = (
        await c.client.get(c.a.fields, headers=c.a.headers, params={"include_archived": True})
    ).json()
    assert archived[0]["archived"] and archived[0]["label"] == "New label"
    # An old open form may resubmit an unchanged archived value, but cannot alter it.
    for attrs, code in [
        ({}, 200),
        ({"color": "Blue"}, 200),
        ({"color": "Red"}, 400),
        ({"color": None}, 400),
    ]:
        response = await c.client.patch(
            product_url, headers=c.a.headers, json={"attributes": attrs}
        )
        assert response.status_code == code, response.text
    saved = (await c.client.get(product_url, headers=c.a.headers)).json()
    assert saved["attributes"] == {"color": "Blue"}
    assert (
        await c.client.post(
            c.a.fields,
            headers=c.a.headers,
            json={
                "key": "color",
                "label": "Reused",
                "field_type": "number",
            },
        )
    ).status_code == 409
    assert (
        await c.client.post(
            c.a.products,
            headers=c.a.headers,
            json={
                "name": "New",
                "price": "1",
                "attributes": {"color": "Blue"},
            },
        )
    ).status_code == 400
    await product(c)


async def test_option_definitions_and_new_required_fields(catalog):
    c = catalog
    p = await product(c)
    f = await field(
        c, field_type="select", required=True, options=[{"value": "blue", "label": "Blue"}]
    )
    response = await c.client.patch(
        c.a.products + "/" + p["id"], headers=c.a.headers, json={"name": "New"}
    )
    assert response.status_code == 400 and "Color is required" in response.text
    url = c.a.fields + "/" + f["id"]
    assert (
        await c.client.patch(
            url,
            headers=c.a.headers,
            json={
                "options": [
                    {"value": "blue", "label": "Ocean"},
                    {"value": "red", "label": "Red"},
                ]
            },
        )
    ).status_code == 200
    assert (
        await c.client.patch(
            url,
            headers=c.a.headers,
            json={
                "options": [
                    {"value": "red", "label": "Red"},
                ]
            },
        )
    ).status_code == 409
    for change in [{"options": None}, {"required": None}, {"label": " "}, {"sort_order": 0.1}]:
        assert (await c.client.patch(url, headers=c.a.headers, json=change)).status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"key": "bad.key"},
        {"key": "constructor"},
        {"key": "__proto__"},
        {"key": "Bad"},
        {"label": " "},
        {"field_type": "unknown"},
        {"field_type": "select"},
        {"field_type": "select", "options": []},
        {"options": [{"value": "x", "label": "X"}]},
        {"business_id": "spoof"},
    ],
)
async def test_invalid_field_definitions(catalog, change):
    response = await catalog.client.post(
        catalog.a.fields,
        headers=catalog.a.headers,
        json={
            "key": "color",
            "label": "Color",
            "field_type": "text",
            **change,
        },
    )
    assert response.status_code == 400


async def test_entity_definitions_are_separate(catalog):
    c = catalog
    f = await field(c, required=True)
    url = c.a.fields.replace("/product", "/service")
    assert (await c.client.get(url, headers=c.a.headers)).json() == []
    assert (
        await c.client.post(
            url,
            headers=c.a.headers,
            json={
                "key": "color",
                "label": "Service Color",
                "field_type": "number",
            },
        )
    ).status_code == 201
    assert (await c.client.delete(url + "/" + f["id"], headers=c.a.headers)).status_code == 404
    assert (
        await c.client.get(url.replace("/service", "/offer"), headers=c.a.headers)
    ).status_code == 400


async def test_pagination_search_categories_sku_and_archiving(catalog):
    c = catalog
    for i in range(5):
        await product(
            c,
            name=f"Shirt {i}",
            sku=f"SKU-{i}",
            category="Clothes",
            status="active" if i % 2 else "inactive",
        )
    target = await product(c, name="100% Cotton_", category="Special", sku="TARGET")
    await product(c, c.b, name="Private", category="Private")
    listing = await c.client.get(c.a.products, headers=c.a.headers, params={"limit": 2})
    data = listing.json()
    assert data["total"] == data["catalog_total"] == 6 and len(data["items"]) == 2
    assert data["categories"] == ["Clothes", "Special"]
    next_page = (
        await c.client.get(c.a.products, headers=c.a.headers, params={"limit": 2, "offset": 2})
    ).json()
    assert not {p["id"] for p in data["items"]} & {p["id"] for p in next_page["items"]}
    for params, count in [
        ({"status": "inactive"}, 3),
        ({"category": "Special"}, 1),
        ({"search": "%"}, 1),
        ({"search": "target"}, 1),
    ]:
        page = (await c.client.get(c.a.products, headers=c.a.headers, params=params)).json()
        assert page["total"] == count
    assert (
        await c.client.post(
            c.a.products,
            headers=c.a.headers,
            json={
                "name": "Duplicate",
                "price": "1",
                "sku": "TARGET",
            },
        )
    ).status_code == 409
    url = c.a.products + "/" + target["id"]
    assert (await c.client.delete(url, headers=c.a.headers)).status_code == 204
    archived = (await c.client.get(url, headers=c.a.headers)).json()
    assert archived["status"] == "archived" and archived["sku"] == "TARGET"
    page = (
        await c.client.get(c.a.products, headers=c.a.headers, params={"status": "archived"})
    ).json()
    assert page["items"] == [archived]
    for values in [
        {"price": "-1"},
        {"price": "1.001"},
        {"price": "NaN"},
        {"name": " "},
        {"currency": "FAKE"},
    ]:
        assert (
            await c.client.post(
                c.a.products, headers=c.a.headers, json={"name": "x", "price": "1", **values}
            )
        ).status_code == 400
    async with c.db.transaction() as session:
        assert len((await session.scalars(select(Product))).all()) == 7
        assert len((await session.scalars(select(FieldDefinition))).all()) == 0
