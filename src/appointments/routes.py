from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from context_agent.schemas import DomainError
from crm.service import owned
from custom_fields.service import Owner
from modules.service import module_transaction

from .models import Appointment
from .schemas import AppointmentCreate, AppointmentOutput, AppointmentUpdate
from .service import check_future, create_appointment

router = APIRouter(prefix="/admin/{slug}/appointments", tags=["Appointments"])


@router.get("")
async def appointments(
    identity: Owner,
    request: Request,
    customer_id: UUID | None = None,
    status: Literal["all", "scheduled", "confirmed", "completed", "cancelled", "no_show"] = "all",
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "appointments"
    ) as session:
        filters = [Appointment.business_id == identity.business.id]
        if customer_id:
            filters.append(Appointment.customer_id == customer_id)
        if status != "all":
            filters.append(Appointment.status == status)
        rows = (
            await session.scalars(
                select(Appointment)
                .where(*filters)
                .order_by(Appointment.scheduled_at.desc(), Appointment.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [AppointmentOutput.model_validate(row) for row in rows],
            "total": await session.scalar(
                select(func.count()).select_from(Appointment).where(*filters)
            ),
        }


@router.get("/{appointment_id}", response_model=AppointmentOutput)
async def detail(appointment_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "appointments"
    ) as session:
        return await owned(session, Appointment, identity.business.id, appointment_id)


@router.post("", response_model=AppointmentOutput, status_code=201)
async def create(payload: AppointmentCreate, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "appointments"
    ) as session:
        return await create_appointment(session, identity.business, payload)


@router.patch("/{appointment_id}", response_model=AppointmentOutput)
async def update(
    appointment_id: UUID, payload: AppointmentUpdate, identity: Owner, request: Request
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "appointments"
    ) as session:
        row = await owned(session, Appointment, identity.business.id, appointment_id)
        changes = payload.model_dump(exclude_unset=True)
        if row.status in {"completed", "cancelled", "no_show"} and any(
            getattr(row, key) != value for key, value in changes.items()
        ):
            raise DomainError(
                409, "invalid_transition", "A finished or cancelled appointment cannot be changed."
            )
        if "scheduled_at" in changes:
            check_future(changes["scheduled_at"])
        for key, value in changes.items():
            setattr(row, key, value)
        await session.flush()
        await session.refresh(row)
        return row
