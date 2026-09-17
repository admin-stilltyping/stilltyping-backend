from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from context_agent.schemas import DomainError
from custom_fields.service import Owner
from super_admin.businesses.service import get_business
from super_admin.models import SuperAdmin
from super_admin.routes import no_cache, require_super_admin

from .models import BusinessModules
from .schemas import ModuleSettings, ModuleUpdate
from .service import entitlement_flags, get_settings

management = APIRouter(prefix="/super-admin/businesses/{slug}/modules", tags=["Business modules"])
portal = APIRouter(tags=["Business modules"])
Platform = Annotated[SuperAdmin, Depends(require_super_admin)]


@management.get("", response_model=ModuleSettings)
async def read_modules(slug: str, account: Platform, request: Request, response: Response):
    no_cache(response)
    async with request.app.state.services["db"].transaction(slug) as session:
        business = await get_business(session, slug)
        return await get_settings(session, business.id)


@management.put("", response_model=ModuleSettings)
async def save_modules(
    slug: str,
    payload: ModuleUpdate,
    account: Platform,
    request: Request,
    response: Response,
):
    no_cache(response)
    selection = payload.selection
    if (selection.product_orders or selection.service_appointments) and not selection.customers:
        raise DomainError(
            400,
            "module_dependency",
            "Customers must remain enabled while Products + Orders or Services + Appointments is enabled.",
        )
    async with request.app.state.services["db"].transaction(slug) as session:
        business = await get_business(session, slug)
        current = await get_settings(session, business.id)
        if current.revision != payload.expected_revision:
            raise DomainError(
                409,
                "module_settings_changed",
                "Module settings changed since you opened this page. Reload the settings and try again.",
            )
        row = await session.get(BusinessModules, business.id)
        if row is None:
            row = BusinessModules(business_id=business.id)
            session.add(row)
        for key, value in selection.model_dump().items():
            setattr(row, key, value)
        row.revision = current.revision + 1
        row.updated_by = account.id
        await session.flush()
        await session.refresh(row)
        return await get_settings(session, business.id)


@portal.get("/admin/businesses/{slug}/entitlements")
async def entitlements(identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        settings = await get_settings(session, identity.business.id)
        return {
            "business_id": identity.business.id,
            "plan": identity.business.plan,
            "flags": entitlement_flags(settings.selection),
            "modules": settings.selection,
            "revision": settings.revision,
        }
