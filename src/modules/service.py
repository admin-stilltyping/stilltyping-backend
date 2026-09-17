from contextlib import asynccontextmanager

from sqlalchemy import select

from context_agent.schemas import DomainError
from super_admin.businesses.models import Business

from .models import BusinessModules
from .schemas import ModuleSelection, ModuleSettings

DEFAULT_SELECTION = {
    "product_orders": False,
    "service_appointments": False,
    "customers": False,
    "leads": False,
    "support_tickets": True,
}
MODULE_GROUP = {
    "products": "product_orders",
    "orders": "product_orders",
    "services": "service_appointments",
    "appointments": "service_appointments",
    "customers": "customers",
    "leads": "leads",
    "support_tickets": "support_tickets",
}


async def get_settings(session, business_id):
    row = await session.get(BusinessModules, business_id)
    return ModuleSettings(
        business_id=business_id,
        selection=ModuleSelection.model_validate(row if row else DEFAULT_SELECTION),
        revision=row.revision if row else 0,
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


async def require_enabled(session, business_id, module):
    settings = await get_settings(session, business_id)
    if not getattr(settings.selection, MODULE_GROUP[module]):
        raise DomainError(
            403,
            "module_disabled",
            "This module is disabled for your business. Contact your super-admin.",
            module=module,
        )


@asynccontextmanager
async def module_transaction(db, business, module):
    # Check inside the same transaction/lock as the read or mutation, so a saved
    # disable cannot race a write that already passed an earlier permission check.
    async with db.transaction(business.slug) as session:
        await require_enabled(session, business.id, module)
        yield session


async def support_enabled(session, tenant):
    business = await session.scalar(select(Business).where(Business.slug == tenant))
    # Standalone agent tenants predate business portals; retain their support fallback.
    if business is None:
        return True
    settings = await get_settings(session, business.id)
    return business.status == "active" and settings.selection.support_tickets


def entitlement_flags(selection):
    return {
        "module.products": selection.product_orders,
        "orders.enabled": selection.product_orders,
        "module.services": selection.service_appointments,
        "module.appointments": selection.service_appointments,
        "module.customers": selection.customers,
        "module.leads": selection.leads,
        "support.tickets_enabled": selection.support_tickets,
        "module.custom_fields": selection.product_orders or selection.service_appointments,
        "module.offers": False,
        "module.coupons": False,
        "ui.agent_runs": True,
        "ui.webhook_events": True,
        "ui.dashboard_customize": True,
    }
