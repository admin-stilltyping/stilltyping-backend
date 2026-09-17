from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, or_, select

from crm.service import owned
from custom_fields.service import Owner, definitions, validate_attributes
from modules.service import module_transaction

from .models import Service
from .schemas import ServiceCreate, ServiceOutput, ServiceStatus, ServiceUpdate

router = APIRouter(prefix="/admin/{slug}/services", tags=["Services"])


@router.get("")
async def services(
    identity: Owner,
    request: Request,
    search: str = Query("", max_length=200),
    status: ServiceStatus | Literal["all"] = "all",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "services"
    ) as session:
        filters = [Service.business_id == identity.business.id]
        if status != "all":
            filters.append(Service.status == status)
        if search.strip():
            filters.append(
                or_(
                    Service.name.icontains(search.strip(), autoescape=True),
                    Service.description.icontains(search.strip(), autoescape=True),
                )
            )
        rows = (
            await session.scalars(
                select(Service)
                .where(*filters)
                .order_by(Service.created_at.desc(), Service.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [ServiceOutput.model_validate(row) for row in rows],
            "total": await session.scalar(
                select(func.count()).select_from(Service).where(*filters)
            ),
        }


@router.get("/{service_id}", response_model=ServiceOutput)
async def detail(service_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "services"
    ) as session:
        return await owned(session, Service, identity.business.id, service_id)


@router.post("", response_model=ServiceOutput, status_code=201)
async def create(payload: ServiceCreate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "services"
    ) as session:
        fields = await definitions(session, identity.business.id, "service", True)
        attributes = validate_attributes(fields, payload.custom_fields)
        row = Service(
            **{**payload.model_dump(), "custom_fields": attributes},
            business_id=identity.business.id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


@router.patch("/{service_id}", response_model=ServiceOutput)
async def update(service_id: UUID, payload: ServiceUpdate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "services"
    ) as session:
        row = await owned(session, Service, identity.business.id, service_id)
        fields = await definitions(session, identity.business.id, "service", True)
        changes = payload.model_dump(exclude_unset=True)
        changes["custom_fields"] = validate_attributes(
            fields, changes.get("custom_fields", {}), row.custom_fields
        )
        for key, value in changes.items():
            setattr(row, key, value or None if key in {"category", "description"} else value)
        await session.flush()
        await session.refresh(row)
        return row


@router.delete("/{service_id}", status_code=204)
async def archive(service_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "services"
    ) as session:
        row = await owned(session, Service, identity.business.id, service_id)
        row.status = "archived"
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
