from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, update

from context_agent.schemas import DomainError
from super_admin.errors import AuthError
from super_admin.routes import no_cache, require_super_admin

from .auth import BusinessIdentity, create_business_token, login_business, require_business_admin
from .models import Business, BusinessAdmin
from .schemas import (
    BusinessBranding,
    BusinessCreate,
    BusinessCreated,
    BusinessLogin,
    BusinessLoginOutput,
    BusinessOutput,
    BusinessPlanUpdate,
    BusinessStatusUpdate,
)
from .service import create_business, get_business, suggest_slug

management = APIRouter(
    prefix="/super-admin/businesses",
    tags=["Businesses"],
    dependencies=[Depends(require_super_admin)],
)
portal = APIRouter(tags=["Business portal"])
BusinessSession = Annotated[BusinessIdentity, Depends(require_business_admin)]


@management.get("/suggest-slug")
async def suggest(request: Request, name: Annotated[str, Query(min_length=1, max_length=200)]):
    return {"slug": await suggest_slug(request.app.state.services["db"], name)}


@management.get("", response_model=list[BusinessOutput])
async def list_businesses(request: Request, response: Response):
    no_cache(response)
    async with request.app.state.services["db"].transaction() as session:
        return list(
            (
                await session.scalars(
                    select(Business).order_by(Business.created_at.desc(), Business.id)
                )
            ).all()
        )


@management.post("", response_model=BusinessCreated, status_code=201)
async def create(payload: BusinessCreate, request: Request, response: Response):
    services = request.app.state.services
    business, username, password = await create_business(
        services["db"], services["settings"], payload
    )
    no_cache(response)
    return BusinessCreated(
        **BusinessOutput.model_validate(business).model_dump(),
        admin_username=username,
        admin_password=password,
    )


@management.get("/{slug}", response_model=BusinessOutput)
async def detail(slug: str, request: Request, response: Response):
    no_cache(response)
    async with request.app.state.services["db"].transaction() as session:
        return await get_business(session, slug)


@management.patch("/{slug}/plan", response_model=BusinessOutput)
async def set_plan(slug: str, payload: BusinessPlanUpdate, request: Request, response: Response):
    no_cache(response)
    async with request.app.state.services["db"].transaction(slug) as session:
        business = await get_business(session, slug)
        business.plan = payload.plan
        await session.flush()
        await session.refresh(business)
        return business


@management.patch("/{slug}/status", response_model=BusinessOutput)
async def set_status(
    slug: str, payload: BusinessStatusUpdate, request: Request, response: Response
):
    no_cache(response)
    async with request.app.state.services["db"].transaction(slug) as session:
        business = await get_business(session, slug)
        if payload.status != "active" and business.status != payload.status:
            await session.execute(
                update(BusinessAdmin)
                .where(BusinessAdmin.business_id == business.id)
                .values(token_version=BusinessAdmin.token_version + 1)
            )
        business.status = payload.status
        await session.flush()
        await session.refresh(business)
        return business


@portal.get("/web/businesses/{slug}", response_model=BusinessBranding)
async def branding(slug: str, request: Request, response: Response):
    no_cache(response)
    async with request.app.state.services["db"].transaction() as session:
        business = await get_business(session, slug)
        if business.status != "active":
            raise DomainError(404, "business_not_found", "Business not found.")
        return business


@portal.post("/auth/login", response_model=BusinessLoginOutput)
async def business_login(payload: BusinessLogin, request: Request, response: Response):
    services = request.app.state.services
    identity = await login_business(services["db"], services["settings"], payload)
    no_cache(response)
    return BusinessLoginOutput(
        access_token=create_business_token(identity, services["settings"]),
        expires_in=services["settings"].super_admin_token_minutes * 60,
        business_id=identity.business.id,
        business_slug=identity.business.slug,
        business_name=identity.business.name,
        username=identity.account.username,
    )


@portal.get("/auth/me")
async def business_me(identity: BusinessSession, response: Response):
    no_cache(response)
    return {
        "username": identity.account.username,
        "role": "admin",
        "business": BusinessOutput.model_validate(identity.business).model_dump(by_alias=True),
    }


@portal.get("/admin/businesses", response_model=list[BusinessOutput])
async def own_business(identity: BusinessSession, response: Response):
    no_cache(response)
    return [identity.business]


@portal.get("/admin/businesses/{slug}", response_model=BusinessOutput)
async def own_business_detail(slug: str, identity: BusinessSession, response: Response):
    if slug != identity.business.slug:
        raise AuthError(403, "tenant_access_denied", "You do not have access to this business.")
    no_cache(response)
    return identity.business
