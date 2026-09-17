from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from context_agent import support
from context_agent.api import create_app
from context_agent.config import Settings
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin

SETTINGS = Settings(_env_file=None, super_admin_jwt_secret="support-test-only-secret-32-bytes-long")


def app_for(db):
    services = {"db": db, "settings": SETTINGS}
    app = create_app(services)
    app.state.services = services  # ASGITransport does not run the lifespan
    return app


async def owner_headers(db, slug):
    async with db.transaction() as session:
        business = Business(slug=slug, name=slug, timezone="UTC")
        session.add(business)
        await session.flush()
        account = BusinessAdmin(business_id=business.id, username=slug, password_hash="unused")
        session.add(account)
        await session.flush()
        token = create_business_token(BusinessIdentity(account, business), SETTINGS)
    return {"Authorization": f"Bearer {token}"}


async def seed(db, tenant, request_id, channel="whatsapp", user="U1"):
    async with db.transaction(tenant) as session:
        ticket = await support.create_or_get(
            session,
            tenant,
            request_id,
            "Why?",
            "No evidence",
            channel=channel,
            external_user_id=user,
        )
        return ticket.ticket_ref


async def test_list_view_and_resolve_ticket(db):
    headers = await owner_headers(db, "acme")
    ref = await seed(db, "acme", uuid4())
    transport = ASGITransport(app=app_for(db))
    async with AsyncClient(transport=transport, base_url="http://t", headers=headers) as client:
        listed = await client.get("/api/v1/tenants/acme/support-tickets")
        viewed = await client.get(f"/api/v1/tenants/acme/support-tickets/{ref}")
        resolved = await client.patch(
            f"/api/v1/tenants/acme/support-tickets/{ref}",
            json={"status": "resolved", "notes": "called the patient"},
        )
        still_open = await client.get(
            "/api/v1/tenants/acme/support-tickets", params={"status": "open"}
        )
        missing = await client.get("/api/v1/tenants/acme/support-tickets/TKT-NOPE0")

    tickets = listed.json()["tickets"]
    assert len(tickets) == 1 and tickets[0]["ticket_ref"] == ref and tickets[0]["status"] == "open"
    assert viewed.json()["channel"] == "whatsapp" and viewed.json()["external_user_id"] == "U1"
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert resolved.json()["notes"] == "called the patient"
    assert resolved.json()["resolved_at"] is not None
    assert still_open.json()["tickets"] == []  # resolved ticket is filtered out
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "ticket_not_found"


async def test_tickets_are_tenant_scoped(db):
    headers = await owner_headers(db, "other")
    ref = await seed(db, "acme", uuid4())
    transport = ASGITransport(app=app_for(db))
    async with AsyncClient(transport=transport, base_url="http://t", headers=headers) as client:
        other_view = await client.get(f"/api/v1/tenants/other/support-tickets/{ref}")
        other_list = await client.get("/api/v1/tenants/other/support-tickets")
    assert other_view.status_code == 404
    assert other_list.json()["tickets"] == []
