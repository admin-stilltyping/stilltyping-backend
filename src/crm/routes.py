from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, or_, select

from custom_fields.service import Owner
from modules.service import module_transaction, require_enabled

from .models import Contact, Customer, Enquiry, Lead, SocialIdentity
from .schemas import EnquiryInput
from .service import customer_view, lead_view, owned, save_enquiry

router = APIRouter(prefix="/admin/{slug}", tags=["Customers and leads"])


def contact_filter(business_id, search):
    matching_social = select(SocialIdentity.contact_id).where(
        SocialIdentity.business_id == business_id,
        SocialIdentity.external_id.icontains(search, autoescape=True),
    )
    return select(Contact.id).where(
        Contact.business_id == business_id,
        or_(Contact.phone.icontains(search, autoescape=True), Contact.id.in_(matching_social)),
    )


@router.get("/customers")
async def customers(
    identity: Owner,
    request: Request,
    search: str = Query("", max_length=200),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "customers"
    ) as session:
        filters = [Customer.business_id == identity.business.id]
        if search.strip():
            filters.append(
                Customer.contact_id.in_(contact_filter(identity.business.id, search.strip()))
            )
        rows = (
            await session.scalars(
                select(Customer)
                .where(*filters)
                .order_by(Customer.created_at.desc(), Customer.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [await customer_view(session, row) for row in rows],
            "total": await session.scalar(
                select(func.count()).select_from(Customer).where(*filters)
            ),
        }


@router.get("/customers/{customer_id}")
async def customer_detail(customer_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "customers"
    ) as session:
        return await customer_view(
            session, await owned(session, Customer, identity.business.id, customer_id)
        )


@router.get("/leads")
async def leads(
    identity: Owner,
    request: Request,
    status: Literal["all", "enquiry", "converted"] = "all",
    search: str = Query("", max_length=200),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "leads"
    ) as session:
        filters = [Lead.business_id == identity.business.id]
        if status != "all":
            filters.append(
                Lead.customer_id.is_(None) if status == "enquiry" else Lead.customer_id.is_not(None)
            )
        if search.strip():
            matching_contacts = contact_filter(identity.business.id, search.strip())
            filters.append(
                or_(
                    Lead.contact_id.in_(matching_contacts),
                    Lead.customer_id.in_(
                        select(Customer.id).where(
                            Customer.business_id == identity.business.id,
                            Customer.contact_id.in_(matching_contacts),
                        )
                    ),
                )
            )
        rows = (
            await session.scalars(
                select(Lead)
                .where(*filters)
                .order_by(Lead.created_at.desc(), Lead.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [await lead_view(session, row) for row in rows],
            "total": await session.scalar(select(func.count()).select_from(Lead).where(*filters)),
        }


@router.get("/leads/{lead_id}")
async def lead_detail(lead_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "leads"
    ) as session:
        return await lead_view(session, await owned(session, Lead, identity.business.id, lead_id))


@router.get("/leads/{lead_id}/enquiries")
async def enquiries(
    lead_id: UUID,
    identity: Owner,
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "leads"
    ) as session:
        await owned(session, Lead, identity.business.id, lead_id)
        filters = [Enquiry.business_id == identity.business.id, Enquiry.lead_id == lead_id]
        rows = (
            await session.scalars(
                select(Enquiry)
                .where(*filters)
                .order_by(Enquiry.created_at.desc(), Enquiry.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "message": row.message,
                    "channel": row.channel,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
            "total": await session.scalar(
                select(func.count()).select_from(Enquiry).where(*filters)
            ),
        }


@router.post("/leads/enquiries", status_code=201)
async def create_enquiry(payload: EnquiryInput, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "leads"
    ) as session:
        return await lead_view(session, await save_enquiry(session, identity.business.id, payload))


@router.get("/customers/{customer_id}/leads")
async def customer_leads(customer_id: UUID, identity: Owner, request: Request):
    async with module_transaction(
        request.app.state.services["db"], identity.business, "customers"
    ) as session:
        await require_enabled(session, identity.business.id, "leads")
        await owned(session, Customer, identity.business.id, customer_id)
        rows = (
            await session.scalars(
                select(Lead)
                .where(Lead.business_id == identity.business.id, Lead.customer_id == customer_id)
                .order_by(Lead.created_at.desc())
                .limit(100)
            )
        ).all()
        return [await lead_view(session, row) for row in rows]
