from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from context_agent.notifications import queue_notification
from context_agent.schemas import DomainError
from crm.service import fingerprint, owned, retry_result, transaction_customer
from custom_fields.service import Owner
from modules.service import module_transaction
from services.models import Service

from .models import Appointment
from .schemas import AppointmentCreate, AppointmentOutput, AppointmentUpdate

router = APIRouter(prefix="/admin/{slug}/appointments", tags=["Appointments"])


def check_future(value):
    if value <= datetime.now(UTC):
        raise DomainError(400, "past_appointment", "Choose a future appointment time.")


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
        previous = await retry_result(session, Appointment, identity.business.id, payload)
        if previous:
            return previous
        check_future(payload.scheduled_at)
        service = await owned(session, Service, identity.business.id, payload.service_id)
        if service.status != "active":
            raise DomainError(
                400, "service_unavailable", "This service is not available for booking."
            )
        customer = await transaction_customer(session, identity.business, payload)
        row = Appointment(
            business_id=identity.business.id,
            customer_id=customer.id,
            lead_id=payload.lead_id,
            service_id=service.id,
            service_name=service.name,
            price=service.price,
            currency=service.currency,
            duration_minutes=service.duration_minutes,
            scheduled_at=payload.scheduled_at,
            notes=payload.notes,
            request_id=payload.request_id,
            request_hash=fingerprint(payload),
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        await queue_notification(
            session, identity.business.id, "appointment", row.id, f"/appointments/{row.id}"
        )
        return row


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
