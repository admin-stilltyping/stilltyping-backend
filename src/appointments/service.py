"""Appointment writes shared by the owner portal and the chat agent."""

from datetime import UTC, datetime

from sqlalchemy import select

from context_agent.notifications import queue_notification
from context_agent.schemas import DomainError
from crm.service import fingerprint, owned, retry_result, transaction_customer
from services.models import Service

from .models import Appointment


def check_future(value):
    if value <= datetime.now(UTC):
        raise DomainError(400, "past_appointment", "Choose a future appointment time.")


async def create_appointment(session, business, payload, *, patient_name=None):
    previous = await retry_result(session, Appointment, business.id, payload)
    if previous:
        return previous
    check_future(payload.scheduled_at)
    service = await owned(session, Service, business.id, payload.service_id)
    if service.status != "active":
        raise DomainError(400, "service_unavailable", "This service is not available for booking.")
    customer = await transaction_customer(session, business, payload)
    if patient_name is not None:
        # A second chat message can repeat a successful booking after a delivery
        # failure. Reuse only the same patient/contact, service and exact time;
        # family members sharing a contact can still have separate appointments.
        previous = await session.scalar(
            select(Appointment)
            .where(
                Appointment.business_id == business.id,
                Appointment.customer_id == customer.id,
                Appointment.service_id == service.id,
                Appointment.scheduled_at == payload.scheduled_at,
                Appointment.status.in_(["scheduled", "confirmed"]),
                Appointment.notes.startswith(f"Patient: {patient_name}\n", autoescape=True),
            )
            .order_by(Appointment.created_at, Appointment.id)
            .limit(1)
        )
        if previous:
            return previous
    row = Appointment(
        business_id=business.id,
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
    await queue_notification(session, business.id, "appointment", row.id, f"/appointments/{row.id}")
    return row
