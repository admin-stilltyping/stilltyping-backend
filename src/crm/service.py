import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from context_agent.schemas import DomainError
from modules.service import get_settings, require_enabled
from super_admin.businesses.models import Business

from .models import Contact, Customer, Enquiry, Lead, LeadSource, SocialIdentity
from .schemas import ContactInput, EnquiryInput, SocialInput


def fingerprint(payload):
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def owned(session, model, business_id, record_id):
    row = await session.scalar(
        select(model).where(model.business_id == business_id, model.id == record_id)
    )
    if row is None:
        raise DomainError(404, "record_not_found", f"{model.__name__} not found.")
    return row


async def retry_result(session, model, business_id, payload):
    row = await session.scalar(
        select(model).where(
            model.business_id == business_id, model.request_id == payload.request_id
        )
    )
    if row and row.request_hash != fingerprint(payload):
        raise DomainError(
            409,
            "request_changed",
            "This request was already saved with different details. Start a new request.",
        )
    return row


async def identities(session, business_id, contact_id):
    return list(
        (
            await session.scalars(
                select(SocialIdentity)
                .where(
                    SocialIdentity.business_id == business_id,
                    SocialIdentity.contact_id == contact_id,
                )
                .order_by(SocialIdentity.platform, SocialIdentity.external_id)
            )
        ).all()
    )


async def has_identity(session, contact):
    return bool(contact.phone or await identities(session, contact.business_id, contact.id))


async def resolve_contact(session, business_id, details, preferred=None):
    """Exact tenant-scoped matching; conflicting identities require manual review.

    Identities are enriched, never removed/overwritten. An anonymous lead can
    transact as an existing contact without discarding its original enquiry history.
    """
    matched = set()
    if details.phone:
        phone_id = await session.scalar(
            select(Contact.id).where(
                Contact.business_id == business_id, Contact.phone == details.phone
            )
        )
        if phone_id:
            matched.add(phone_id)
    for item in details.social_identities:
        contact_id = await session.scalar(
            select(SocialIdentity.contact_id).where(
                SocialIdentity.business_id == business_id,
                SocialIdentity.platform == item.platform,
                SocialIdentity.external_id == item.external_id,
            )
        )
        if contact_id:
            matched.add(contact_id)
    if preferred and await has_identity(session, preferred):
        matched.add(preferred.id)
    if len(matched) > 1:
        raise DomainError(
            409,
            "identity_conflict",
            "These contact identities belong to different people. Check the phone and social IDs.",
        )
    row = await owned(session, Contact, business_id, next(iter(matched))) if matched else preferred
    if row is None:
        row = Contact(business_id=business_id)
        session.add(row)
        await session.flush()
    if details.phone:
        if row.phone and row.phone != details.phone:
            raise DomainError(
                409,
                "identity_conflict",
                "This contact already has another phone number. Check the selected person.",
            )
        row.phone = details.phone
    existing = {
        (item.platform, item.external_id) for item in await identities(session, business_id, row.id)
    }
    for item in details.social_identities:
        if (item.platform, item.external_id) not in existing:
            session.add(
                SocialIdentity(business_id=business_id, contact_id=row.id, **item.model_dump())
            )
    await session.flush()
    return row


async def contact_view(session, business_id, contact_id):
    row = await owned(session, Contact, business_id, contact_id)
    return {
        "phone": row.phone,
        "social_identities": [
            {"platform": item.platform, "external_id": item.external_id}
            for item in await identities(session, business_id, contact_id)
        ],
    }


async def customer_view(session, row):
    return {
        "id": row.id,
        "business_id": row.business_id,
        "created_at": row.created_at,
        **await contact_view(session, row.business_id, row.contact_id),
    }


async def lead_view(session, row):
    contact_id = row.contact_id
    if row.customer_id:
        customer = await owned(session, Customer, row.business_id, row.customer_id)
        contact_id = customer.contact_id
    count = await session.scalar(
        select(func.count())
        .select_from(Enquiry)
        .where(Enquiry.business_id == row.business_id, Enquiry.lead_id == row.id)
    )
    latest = await session.scalar(
        select(Enquiry)
        .where(Enquiry.business_id == row.business_id, Enquiry.lead_id == row.id)
        .order_by(Enquiry.created_at.desc(), Enquiry.id.desc())
        .limit(1)
    )
    return {
        "id": row.id,
        "business_id": row.business_id,
        "customer_id": row.customer_id,
        "status": "converted" if row.customer_id else "enquiry",
        "converted_at": row.converted_at,
        "created_at": row.created_at,
        "enquiry_count": count,
        "latest_message": latest.message if latest else None,
        "last_enquiry_at": latest.created_at if latest else None,
        **await contact_view(session, row.business_id, contact_id),
    }


async def save_enquiry(session, business_id, payload, source=None):
    previous = await retry_result(session, Enquiry, business_id, payload)
    if previous:
        return await owned(session, Lead, business_id, previous.lead_id)
    lead = await owned(session, Lead, business_id, payload.lead_id) if payload.lead_id else None
    if source and lead is None:
        lead_id = await session.scalar(
            select(LeadSource.lead_id).where(
                LeadSource.business_id == business_id,
                LeadSource.channel == source[0],
                LeadSource.external_id == source[1],
            )
        )
        if lead_id:
            lead = await owned(session, Lead, business_id, lead_id)
    preferred = await owned(session, Contact, business_id, lead.contact_id) if lead else None
    contact = await resolve_contact(session, business_id, payload.contact, preferred)
    if lead and preferred and contact.id != preferred.id:
        raise DomainError(
            409,
            "identity_conflict",
            "These details belong to another contact. Capture a new enquiry for that contact, or use the details when creating this lead's order or appointment.",
        )
    if lead is None:
        lead = await session.scalar(
            select(Lead).where(Lead.business_id == business_id, Lead.contact_id == contact.id)
        )
    if lead is None:
        customer = await session.scalar(
            select(Customer).where(
                Customer.business_id == business_id, Customer.contact_id == contact.id
            )
        )
        lead = Lead(
            business_id=business_id,
            contact_id=contact.id,
            customer_id=customer.id if customer else None,
            converted_at=customer.created_at if customer else None,
        )
        session.add(lead)
        await session.flush()
    if source:
        source_row = await session.scalar(
            select(LeadSource).where(
                LeadSource.business_id == business_id,
                LeadSource.channel == source[0],
                LeadSource.external_id == source[1],
            )
        )
        if source_row is None:
            session.add(
                LeadSource(
                    business_id=business_id,
                    lead_id=lead.id,
                    channel=source[0],
                    external_id=source[1],
                )
            )
    session.add(
        Enquiry(
            business_id=business_id,
            lead_id=lead.id,
            request_id=payload.request_id,
            request_hash=fingerprint(payload),
            channel=payload.channel,
            message=payload.message,
        )
    )
    await session.flush()
    return lead


async def transaction_customer(session, business, payload):
    await require_enabled(session, business.id, "customers")
    if payload.customer_id:
        return await owned(session, Customer, business.id, payload.customer_id)
    lead = None
    preferred = None
    if payload.lead_id:
        await require_enabled(session, business.id, "leads")
        lead = await owned(session, Lead, business.id, payload.lead_id)
        if lead.customer_id:
            customer = await owned(session, Customer, business.id, lead.customer_id)
            preferred = await owned(session, Contact, business.id, customer.contact_id)
        else:
            preferred = await owned(session, Contact, business.id, lead.contact_id)
    contact = await resolve_contact(
        session, business.id, payload.contact or ContactInput(), preferred
    )
    if not await has_identity(session, contact):
        raise DomainError(
            400,
            "contact_required",
            "A phone number or social identity is required before creating a customer.",
        )
    customer = await session.scalar(
        select(Customer).where(
            Customer.business_id == business.id, Customer.contact_id == contact.id
        )
    )
    if customer is None:
        customer = Customer(business_id=business.id, contact_id=contact.id)
        session.add(customer)
        await session.flush()
    # Also convert an existing lead when a transaction is entered using its contact
    # details rather than selecting the lead explicitly.
    leads = list(
        (
            await session.scalars(
                select(Lead).where(Lead.business_id == business.id, Lead.contact_id == contact.id)
            )
        ).all()
    )
    if lead and all(item.id != lead.id for item in leads):
        leads.append(lead)
    for item in leads:
        if item.customer_id and item.customer_id != customer.id:
            raise DomainError(
                409, "identity_conflict", "This lead is already linked to another customer."
            )
        if not item.customer_id:
            item.customer_id = customer.id
            item.converted_at = datetime.now(UTC)
    await session.flush()
    return customer


async def capture_incoming(db, tenant, request):
    """Persist incoming enquiries before the model runs, even when answering fails."""
    async with db.transaction(tenant) as session:
        business = await session.scalar(select(Business).where(Business.slug == tenant))
        if business is None or business.status != "active":
            return
        settings = await get_settings(session, business.id)
        if not settings.selection.leads:
            return
        channel = (
            request.channel
            if request.channel in {"web", "whatsapp", "telegram", "instagram", "facebook"}
            else "web"
        )
        social = []
        if channel != "web" and request.external_user_id:
            social = [SocialInput(platform=channel, external_id=request.external_user_id)]
        payload = EnquiryInput(
            request_id=request.request_id,
            message=request.message,
            channel=channel,
            contact=ContactInput(social_identities=social),
        )
        # Source is included in the idempotency hash through contact for social
        # channels. For web, bind the request id to its session explicitly below.
        source = (channel, request.external_user_id) if request.external_user_id else None
        if source and channel == "web":
            previous = await session.scalar(
                select(Enquiry).where(
                    Enquiry.business_id == business.id, Enquiry.request_id == request.request_id
                )
            )
            if previous:
                match = await session.scalar(
                    select(LeadSource.id).where(
                        LeadSource.business_id == business.id,
                        LeadSource.lead_id == previous.lead_id,
                        LeadSource.channel == channel,
                        LeadSource.external_id == source[1],
                    )
                )
                if not match:
                    raise DomainError(
                        409, "request_changed", "Request ID belongs to a different enquiry session."
                    )
        await save_enquiry(session, business.id, payload, source)
