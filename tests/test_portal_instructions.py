from httpx import ASGITransport, AsyncClient

from context_agent.api import create_app
from context_agent.config import Settings
from super_admin.businesses.auth import BusinessIdentity, create_business_token
from super_admin.businesses.models import Business, BusinessAdmin


async def test_portal_instructions_require_owner_and_preserve_server_key_boundary(db):
    settings = Settings(
        _env_file=None,
        tenant_api_key="test-server-only-key",
        super_admin_jwt_secret="instructions-test-signing-secret-32-bytes",
    )
    async with db.transaction() as session:
        business = Business(slug="clinic", name="Clinic", timezone="UTC")
        session.add(business)
        await session.flush()
        account = BusinessAdmin(business_id=business.id, username="owner", password_hash="unused")
        session.add(account)
        await session.flush()
        token = create_business_token(BusinessIdentity(account, business), settings)
    services = {"db": db, "settings": settings}
    app = create_app(services)
    app.state.services = services
    owner = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method in ("GET", "PUT"):
            for headers in ({}, {"X-Tenant-API-Key": "test-server-only-key"}):
                response = await client.request(
                    method, "/admin/clinic/instructions", headers=headers,
                    json={"instructions": "Be helpful"} if method == "PUT" else None,
                )
                assert response.status_code == 401
            response = await client.request(
                method, "/admin/other/instructions", headers=owner,
                json={"instructions": "Be helpful"} if method == "PUT" else None,
            )
            assert response.status_code == 403
        response = await client.put(
            "/admin/clinic/instructions", headers=owner, json={"instructions": "Be helpful"}
        )
        assert response.status_code == 200
        response = await client.get("/admin/clinic/instructions", headers=owner)
        assert response.status_code == 200
        assert response.json()["instructions"] == "Be helpful"
        assert (await client.get("/api/v1/tenants/clinic/instructions", headers=owner)).status_code == 401
        response = await client.get(
            "/api/v1/tenants/clinic/instructions", headers={"X-Tenant-API-Key": "test-server-only-key"}
        )
        assert response.status_code == 200
