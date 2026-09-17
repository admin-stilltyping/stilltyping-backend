from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.agent import Agent
from context_agent.api import create_app
from context_agent.config import Settings
from context_agent.schemas import ChatInput
from crm.models import Contact, Customer, Enquiry, Lead
from crm.service import capture_incoming
from modules.models import BusinessModules
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import BusinessAdmin
from super_admin.businesses.schemas import BusinessCreate
from super_admin.businesses.service import create_business


@pytest.fixture
async def crm(db):
    settings = Settings(
        _env_file=None, super_admin_jwt_secret="crm-test-only-signing-secret-long-enough"
    )
    people = []
    for name in ("CRM A", "CRM B"):
        business, _, _ = await create_business(db, settings, BusinessCreate(name=name))
        async with db.transaction() as session:
            modules = await session.get(BusinessModules, business.id)
            modules.product_orders = modules.service_appointments = modules.customers = (
                modules.leads
            ) = True
            admin = await session.scalar(
                select(BusinessAdmin).where(BusinessAdmin.business_id == business.id)
            )
        people.append(
            SimpleNamespace(
                business=business,
                headers={
                    "Authorization": "Bearer "
                    + create_business_token(BusinessIdentity(admin, business), settings)
                },
            )
        )
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(db=db, settings=settings, client=client, a=people[0], b=people[1])


async def call(c, method, path, body=None, owner=None, code=200):
    person = owner or c.a
    response = await c.client.request(
        method, f"/admin/{person.business.slug}{path}", headers=person.headers, json=body
    )
    assert response.status_code == code, response.text
    return response.json() if response.content else None


async def lead(c, phone="+919876543210", **changes):
    return await call(
        c,
        "POST",
        "/leads/enquiries",
        {
            "request_id": str(uuid4()),
            "message": "What is available?",
            "contact": {"phone": phone},
            **changes,
        },
        code=201,
    )


async def product(c, **changes):
    return await call(
        c, "POST", "/products", {"name": "Test product", "price": "10.25", **changes}, code=201
    )


async def service(c, **changes):
    return await call(
        c,
        "POST",
        "/services",
        {"name": "Consultation", "price": "75.00", "duration_minutes": 45, **changes},
        code=201,
    )


def order_body(p, **recipient):
    return {
        "request_id": str(uuid4()),
        "items": [{"product_id": p["id"], "quantity": 2}],
        **recipient,
    }


def appointment_body(s, **recipient):
    return {
        "request_id": str(uuid4()),
        "service_id": s["id"],
        "scheduled_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        **recipient,
    }


async def count(c, model):
    async with c.db.transaction() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_enquiries_are_not_customers_and_are_deduplicated(crm):
    c = crm
    request_id = str(uuid4())
    first = await lead(c, "+91 (98765) 43210", request_id=request_id)
    same = await lead(c, "+919876543210", request_id=request_id)
    assert first["id"] == same["id"] and same["enquiry_count"] == 1
    again = await lead(c, message="Second enquiry")
    assert again["id"] == first["id"] and again["enquiry_count"] == 2
    assert again["status"] == "enquiry" and again["customer_id"] is None
    assert await count(c, Customer) == 0
    await call(
        c,
        "POST",
        "/leads/enquiries",
        {"request_id": request_id, "message": "changed", "contact": {"phone": "+919876543210"}},
        code=409,
    )
    assert (await call(c, "GET", f"/leads/{first['id']}/enquiries"))["total"] == 2


async def test_order_converts_once_and_appointment_reuses_customer(crm):
    c = crm
    lead_record, p, s = await lead(c), await product(c), await service(c)
    o = await call(c, "POST", "/orders", order_body(p, lead_id=lead_record["id"]), code=201)
    assert o["total"] == "20.50"
    customer = await call(c, "GET", f"/customers/{o['customer_id']}")
    assert (
        customer["phone"] == "+919876543210" and "email" not in customer and "name" not in customer
    )
    converted = await call(c, "GET", f"/leads/{lead_record['id']}")
    assert converted["status"] == "converted" and converted["customer_id"] == customer["id"]
    a = await call(
        c,
        "POST",
        "/appointments",
        appointment_body(s, contact={"phone": "+91 98765 43210"}),
        code=201,
    )
    assert a["customer_id"] == customer["id"] and a["duration_minutes"] == 45
    assert await count(c, Customer) == 1
    assert (await call(c, "GET", f"/leads/{lead_record['id']}"))["converted_at"] == converted["converted_at"]
    assert (await lead(c, message="Another enquiry"))["customer_id"] == customer["id"]
    assert (await call(c, "GET", "/customers"))["total"] == 1


async def test_snapshots_retries_and_cancellation_preserve_customer(crm):
    c = crm
    p, s = await product(c), await service(c)
    body = order_body(
        p, contact={"social_identities": [{"platform": "instagram", "external_id": "12345"}]}
    )
    o = await call(c, "POST", "/orders", body, code=201)
    await call(
        c,
        "PATCH",
        f"/products/{p['id']}",
        {"name": "New name", "price": "99.00", "status": "archived"},
    )
    assert await call(c, "POST", "/orders", body, code=201) == o
    await call(
        c, "POST", "/orders", {**body, "items": [{"product_id": p["id"], "quantity": 3}]}, code=409
    )
    cancelled = await call(c, "PATCH", f"/orders/{o['id']}", {"status": "cancelled"})
    assert cancelled["items"][0]["product_name"] == "Test product" and cancelled["total"] == "20.50"
    await call(c, "PATCH", f"/orders/{o['id']}", {"status": "fulfilled"}, code=409)
    ab = appointment_body(s, customer_id=o["customer_id"])
    a = await call(c, "POST", "/appointments", ab, code=201)
    await call(
        c,
        "PATCH",
        f"/services/{s['id']}",
        {"name": "Changed", "price": "100.00", "duration_minutes": 60, "status": "archived"},
    )
    assert await call(c, "POST", "/appointments", ab, code=201) == a
    assert (
        a["service_name"] == "Consultation"
        and a["price"] == "75.00"
        and a["duration_minutes"] == 45
    )
    await call(c, "PATCH", f"/appointments/{a['id']}", {"status": "cancelled"})
    await call(c, "PATCH", f"/appointments/{a['id']}", {"status": "confirmed"}, code=409)
    assert await count(c, Customer) == 1


async def test_failed_transactions_do_not_convert_or_leave_contacts(crm):
    c = crm
    lead_record, p, s = (
        await lead(c),
        await product(c, status="inactive"),
        await service(c, status="archived"),
    )
    for path, body in [
        ("/orders", order_body(p, lead_id=lead_record["id"])),
        ("/appointments", appointment_body(s, lead_id=lead_record["id"])),
    ]:
        await call(c, "POST", path, body, code=400)
    assert await count(c, Customer) == 0
    assert (await call(c, "GET", f"/leads/{lead_record['id']}"))["status"] == "enquiry"
    p = await product(c)
    await call(c, "POST", "/orders", order_body(p, contact={}), code=400)
    assert await count(c, Contact) == 1  # failed new anonymous contact was rolled back
    usd = await product(c, currency="USD")
    body = order_body(p, lead_id=lead_record["id"])
    body["items"].append({"product_id": usd["id"], "quantity": 1})
    await call(c, "POST", "/orders", body, code=400)
    assert await count(c, Customer) == 0


async def test_anonymous_enquiry_requires_identity_then_reuses_customer(crm):
    c = crm
    p = await product(c)
    known = await call(
        c, "POST", "/orders", order_body(p, contact={"phone": "+919876543210"}), code=201
    )
    anonymous = await lead(c, phone=None)
    body = order_body(p, lead_id=anonymous["id"])
    await call(c, "POST", "/orders", body, code=400)
    result = await call(
        c, "POST", "/orders", {**body, "contact": {"phone": "+919876543210"}}, code=201
    )
    assert result["customer_id"] == known["customer_id"]
    matching = await call(c, "GET", "/leads?search=9876543210")
    assert matching["total"] == 1 and matching["items"][0]["id"] == anonymous["id"]
    assert (await call(c, "GET", f"/leads/{anonymous['id']}"))["customer_id"] == known[
        "customer_id"
    ]
    assert await count(c, Customer) == 1


async def test_identity_conflict_does_not_merge_people(crm):
    c = crm
    p = await product(c)
    first = await lead(c)
    second = await lead(
        c,
        phone=None,
        contact={"social_identities": [{"platform": "telegram", "external_id": "555"}]},
    )
    await call(
        c,
        "POST",
        "/orders",
        order_body(
            p,
            lead_id=first["id"],
            contact={"social_identities": [{"platform": "telegram", "external_id": "555"}]},
        ),
        code=409,
    )
    assert await count(c, Customer) == 0
    assert (await call(c, "GET", f"/leads/{second['id']}"))["customer_id"] is None


async def test_tenant_isolation_on_all_related_ids(crm):
    c = crm
    p, s, lead_record = await product(c), await service(c), await lead(c)
    o = await call(c, "POST", "/orders", order_body(p, lead_id=lead_record["id"]), code=201)
    for path in (
        f"/products/{p['id']}",
        f"/services/{s['id']}",
        f"/leads/{lead_record['id']}",
        f"/leads/{lead_record['id']}/enquiries",
        f"/customers/{o['customer_id']}",
        f"/orders/{o['id']}",
    ):
        await call(c, "GET", path, owner=c.b, code=404)
    await call(
        c, "POST", "/orders", order_body(p, contact={"phone": "+919876543210"}), owner=c.b, code=404
    )
    await call(
        c,
        "POST",
        "/appointments",
        appointment_body(s, contact={"phone": "+919876543210"}),
        owner=c.b,
        code=404,
    )
    pb = await call(c, "POST", "/products", {"name": "B", "price": "1.00"}, owner=c.b, code=201)
    for recipient in ({"lead_id": lead_record["id"]}, {"customer_id": o["customer_id"]}):
        await call(c, "POST", "/orders", order_body(pb, **recipient), owner=c.b, code=404)
    ob = await call(
        c,
        "POST",
        "/orders",
        order_body(pb, contact={"phone": "+919876543210"}),
        owner=c.b,
        code=201,
    )
    assert ob["customer_id"] != o["customer_id"]
    url = f"/admin/{c.a.business.slug}/customers"
    assert (await c.client.get(url)).status_code == 401
    assert (await c.client.get(url, headers=c.b.headers)).status_code == 403


async def test_service_custom_fields_and_validation(crm):
    c = crm
    await call(
        c,
        "POST",
        "/custom-fields/service",
        {"key": "room", "label": "Room", "field_type": "text", "required": True},
        code=201,
    )
    await call(c, "POST", "/services", {"name": "Consultation", "price": "10"}, code=400)
    s = await service(c, custom_fields={"room": "A"})
    s = await call(
        c, "PATCH", f"/services/{s['id']}", {"category": "", "custom_fields": {"room": "B"}}
    )
    assert s["category"] is None and s["custom_fields"] == {"room": "B"}
    for changes in (
        {"duration_minutes": 0},
        {"duration_minutes": True},
        {"price": "0.001"},
        {"custom_fields": {"unknown": "x"}},
    ):
        await call(c, "PATCH", f"/services/{s['id']}", changes, code=400)


async def test_incoming_capture_survives_model_failure_and_dedups(crm):
    c = crm
    request = ChatInput(
        request_id=uuid4(), channel="web", external_user_id="visitor-123", message="Can I book?"
    )

    class Failure:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    agent = Agent.__new__(Agent)
    agent.db, agent.settings, agent.graph = c.db, c.settings, Failure()
    with pytest.raises(RuntimeError, match="model unavailable"):
        await agent.run(c.a.business.slug, request)
    await capture_incoming(c.db, c.a.business.slug, request)
    await capture_incoming(
        c.db,
        c.a.business.slug,
        request.model_copy(update={"request_id": uuid4(), "message": "Another message"}),
    )
    assert (
        await count(c, Lead) == 1 and await count(c, Enquiry) == 2 and await count(c, Customer) == 0
    )
    social = ChatInput(
        request_id=uuid4(), channel="instagram", external_user_id="123", message="How much?"
    )
    await capture_incoming(c.db, c.a.business.slug, social)
    data = (await call(c, "GET", "/leads"))["items"]
    assert any(
        item["social_identities"] == [{"platform": "instagram", "external_id": "123"}]
        for item in data
    )


async def test_disabled_modules_block_apis_and_capture(crm):
    c = crm
    async with c.db.transaction() as session:
        modules = await session.get(BusinessModules, c.a.business.id)
        modules.product_orders = modules.service_appointments = modules.customers = (
            modules.leads
        ) = False
    for path in ("/customers", "/leads", "/services", "/orders", "/appointments"):
        await call(c, "GET", path, code=403)
    await capture_incoming(c.db, c.a.business.slug, ChatInput(request_id=uuid4(), message="Hello"))
    assert await count(c, Lead) == 0


@pytest.mark.parametrize(
    "contact",
    [
        {"phone": "9876543210"},
        {"phone": "+123"},
        {"social_identities": [{"platform": "unknown", "external_id": "1"}]},
    ],
)
async def test_invalid_contact_rejected(crm, contact):
    await call(
        crm,
        "POST",
        "/leads/enquiries",
        {"request_id": str(uuid4()), "message": "Hi", "contact": contact},
        code=400,
    )


async def test_search_pagination_and_time_validation(crm):
    c = crm
    await lead(c)
    await lead(c, "+12025550123")
    assert (await call(c, "GET", "/leads?limit=1"))["total"] == 2
    assert len((await call(c, "GET", "/leads?limit=1&offset=1"))["items"]) == 1
    assert (await call(c, "GET", "/leads?search=202555"))["total"] == 1
    s = await service(c)
    for when in ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00"):
        body = appointment_body(s, contact={"phone": "+12025550123"})
        body["scheduled_at"] = when
        await call(c, "POST", "/appointments", body, code=400)
    assert await count(c, Customer) == 0
