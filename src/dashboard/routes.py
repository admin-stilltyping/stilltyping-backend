import secrets
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response

from super_admin.businesses.auth import BusinessIdentity, require_business_admin
from super_admin.routes import no_cache

from .catalog import USE_CASES, catalog_for
from .schemas import DashboardOutput, GenerateInput, Period, SaveInput, UseCase
from .service import check_revision, choose_widgets, get_config, render_dashboard

router = APIRouter(prefix="/admin/dashboard", tags=["Dashboard"])
Session = Annotated[BusinessIdentity, Depends(require_business_admin)]
Days = Annotated[Period, Query()]


@router.get("/catalog")
async def catalog(identity: Session, response: Response, use_case: UseCase = "support"):
    no_cache(response)
    return {"mode": "demo", "use_cases": USE_CASES, "widgets": catalog_for(use_case)}


@router.get("", response_model=DashboardOutput)
async def overview(request: Request, response: Response, identity: Session, days: Days = 30):
    no_cache(response)
    # First access initializes a single saved layout; the tenant lock also covers concurrent tabs.
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        config = await get_config(session, identity.business)
        return render_dashboard(identity.business, config, days)


@router.post("/generate", response_model=DashboardOutput)
async def generate(
    payload: GenerateInput, request: Request, response: Response, identity: Session, days: Days = 30
):
    no_cache(response)
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        config = await get_config(session, identity.business)
        check_revision(config, payload.expected_revision)
        seed = secrets.randbelow(2**31)
        config.widget_ids = choose_widgets(seed, config.widget_ids)
        config.demo_seed = seed
        config.use_case = payload.use_case
        config.as_of = datetime.now(ZoneInfo(identity.business.timezone)).date()
        config.revision += 1
        await session.flush()
        return render_dashboard(identity.business, config, days)


@router.patch("/config", response_model=DashboardOutput)
async def save(
    payload: SaveInput, request: Request, response: Response, identity: Session, days: Days = 30
):
    no_cache(response)
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        config = await get_config(session, identity.business)
        check_revision(config, payload.expected_revision)
        config.widget_ids = payload.widget_ids
        config.revision += 1
        await session.flush()
        return render_dashboard(identity.business, config, days)
