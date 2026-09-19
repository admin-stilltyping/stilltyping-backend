"""Tenant-scoped chat booking. Identity and prices never come from model arguments."""

from datetime import UTC, datetime
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, field_validator
from sqlalchemy import func, or_, select

from context_agent.schemas import DomainError, StrictModel
from crm.models import LeadSource
from crm.schemas import ContactInput, SocialInput
from modules.service import get_settings, require_enabled
from services.models import Service
from super_admin.businesses.models import Business

from .schemas import AppointmentCreate
from .service import create_appointment

SOCIAL_CHANNELS = {"whatsapp", "instagram", "telegram", "facebook"}


class CatalogInput(StrictModel):
    search: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, strict=True)


class BookingInput(StrictModel):
    service_id: UUID
    patient_name: str = Field(min_length=1, max_length=200)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    phone: str | None = Field(default=None, max_length=40)
    concern: str = Field(min_length=1, max_length=2000)

    @field_validator("patient_name", "concern")
    @classmethod
    def meaningful_text(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Supply non-empty text without control characters.")
        return value


async def booking_business(session, tenant):
    business = await session.scalar(select(Business).where(Business.slug == tenant))
    if business is None or business.status != "active":
        raise DomainError(403, "booking_unavailable", "Online booking is unavailable.")
    await require_enabled(session, business.id, "appointments")
    await require_enabled(session, business.id, "customers")
    return business


async def booking_enabled(session, tenant):
    try:
        await booking_business(session, tenant)
    except DomainError:
        return False
    return True


def local_booking_time(date, time, timezone):
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        zone = ZoneInfo(timezone)
        first, second = (naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1))
        # Do not silently move nonexistent DST times or choose between repeated hours.
        if (
            first.utcoffset() != second.utcoffset()
            or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
        ):
            raise ValueError("Ambiguous or nonexistent local time")
        return first.astimezone(UTC)
    except ValueError:
        raise DomainError(
            400, "invalid_booking_time", "Choose a valid, unambiguous local date and time."
        ) from None


async def list_services(args, context):
    if context.db is None:
        return {"ok": False, "error": "Appointment storage is unavailable."}
    query = CatalogInput.model_validate(args)
    async with context.db.transaction(context.tenant_id) as session:
        business = await booking_business(session, context.tenant_id)
        filters = [Service.business_id == business.id, Service.status == "active"]
        if query.search.strip():
            filters.append(
                or_(
                    Service.name.icontains(query.search.strip(), autoescape=True),
                    Service.description.icontains(query.search.strip(), autoescape=True),
                )
            )
        rows = list(
            (
                await session.scalars(
                    select(Service)
                    .where(*filters)
                    .order_by(Service.name, Service.id)
                    .offset(query.offset)
                    .limit(20)
                )
            ).all()
        )
        total = await session.scalar(select(func.count()).select_from(Service).where(*filters))
        return {
            "ok": True,
            "timezone": business.timezone,
            "current_datetime": datetime.now(ZoneInfo(business.timezone)).isoformat(),
            "services": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "description": (row.description or "")[:500],
                    "price": str(row.price),
                    "currency": row.currency,
                    "duration_minutes": row.duration_minutes,
                }
                for row in rows
            ],
            "total": total,
            "next_offset": query.offset + 20 if query.offset + 20 < total else None,
            "requires_staff_confirmation": True,
            "availability_checked": False,
        }


async def book(args, context):
    if context.db is None:
        return {"ok": False, "error": "Appointment storage is unavailable."}
    try:
        booking = BookingInput.model_validate(args)
        # Only the server's actual sender identity is attached. Web/admin session
        # IDs are not social contacts and require a patient-supplied phone number.
        social = (
            [SocialInput(platform=context.channel, external_id=context.external_user_id)]
            if context.channel in SOCIAL_CHANNELS and context.external_user_id
            else []
        )
        contact = ContactInput(phone=booking.phone, social_identities=social)
    except ValidationError as exc:
        return {
            "ok": False,
            "code": "invalid_booking_details",
            "error": "Check the patient name, service, date, time and international phone number.",
            "fields": sorted({str(item["loc"][0]) for item in exc.errors()}),
        }
    if not contact.phone and not social:
        return {
            "ok": False,
            "code": "contact_required",
            "error": "Ask for the patient's phone number including country code before booking.",
        }
    async with context.db.transaction(context.tenant_id) as session:
        business = await booking_business(session, context.tenant_id)
        scheduled_at = local_booking_time(booking.date, booking.time, business.timezone)
        # The same turn can create at most one appointment even if a model retries
        # with a different tool call ID. Different conversation identities cannot
        # reuse one another's idempotency keys.
        request_id = uuid5(
            context.request_id,
            f"appointment:{context.channel}:{context.external_user_id or ''}",
        )
        lead_id = None
        settings = await get_settings(session, business.id)
        if settings.selection.leads and context.external_user_id:
            lead_id = await session.scalar(
                select(LeadSource.lead_id).where(
                    LeadSource.business_id == business.id,
                    LeadSource.channel == context.channel,
                    LeadSource.external_id == context.external_user_id,
                )
            )
        payload = AppointmentCreate(
            request_id=request_id,
            service_id=booking.service_id,
            scheduled_at=scheduled_at,
            contact=contact,
            lead_id=lead_id,
            notes=(
                f"Patient: {booking.patient_name}\n"
                f"Channel: {context.channel}\nConcern: {booking.concern}"
            ),
        )
        row = await create_appointment(
            session, business, payload, patient_name=booking.patient_name
        )
        local = (
            row.scheduled_at.replace(tzinfo=UTC)
            if row.scheduled_at.tzinfo is None
            else row.scheduled_at
        )
        local = local.astimezone(ZoneInfo(business.timezone))
        result = {
            "ok": True,
            "appointment_id": str(row.id),
            "patient_name": booking.patient_name,
            "service": row.service_name,
            "scheduled_at": local.isoformat(),
            "timezone": business.timezone,
            "status": row.status,
            "price": str(row.price),
            "currency": row.currency,
            "requires_staff_confirmation": row.status == "scheduled",
            "availability_checked": False,
        }
    # A success response is constructed only after the transaction commits.
    return result
