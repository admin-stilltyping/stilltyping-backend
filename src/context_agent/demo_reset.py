"""Temporary, account-gated Instagram demo reset. Never called by an LLM tool."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakKeyDictionary, WeakValueDictionary

from sqlalchemy import delete, or_, select, text

from appointments.models import Appointment
from crm.models import Contact, Customer, Enquiry, Lead, LeadSource, SocialIdentity
from orders.models import Order
from super_admin.businesses.models import Business

from .db import (
    AiUsageRecord,
    Conversation,
    InstagramDemoReset,
    Message,
    SupportTicket,
    WebhookEvent,
)
from .notification_models import Notification, PushDelivery

CONFIRMATION = (
    "Demo reset complete. Your backend conversation and linked demo records have been cleared. "
    "Send a new message to start fresh. Instagram's message history will still be visible."
)
_locks = WeakKeyDictionary()


def enabled(settings, channel, config):
    return channel == "instagram" and str(config.get("account_id", "")) in getattr(
        settings, "instagram_demo_clear_accounts", set()
    )


def is_command(message):
    return message.strip().casefold() == "/clear"


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@asynccontextmanager
async def sender_lock(db, tenant, sender):
    """Serialize enabled demo turns and resets, including model/tool/send work.

    A transaction-scoped advisory lock works through Supabase's transaction pooler.
    Waiting workers release their connections between attempts. A separate lock
    namespace avoids the tenant locks used by the short persistence transactions.
    SQLite/local workers use a weakly held asyncio lock instead.
    """
    key = (tenant, sender)
    local = _locks.setdefault(db, WeakValueDictionary())
    lock = local.setdefault(key, asyncio.Lock())
    async with lock:
        if db.engine.dialect.name != "postgresql":
            yield
            return
        number = int.from_bytes(
            hashlib.sha256(f"instagram-demo:{tenant}:{sender}".encode()).digest()[:8],
            signed=True,
        )
        async with asyncio.timeout(180):
            while True:
                async with db.transaction() as session:
                    acquired = await session.scalar(
                        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": number}
                    )
                    if acquired:
                        yield
                        return
                await asyncio.sleep(0.1)


async def predates_reset(db, tenant, sender, received_at):
    async with db.transaction() as session:
        row = await session.get(InstagramDemoReset, (tenant, sender))
        return row is not None and aware(received_at) <= aware(row.cleared_at)


async def _ids(session, model, *conditions):
    return set(await session.scalars(select(model.id).where(*conditions)))


async def _has(session, model, business_id, **fields):
    return (
        await session.scalar(
            select(model.id)
            .where(
                model.business_id == business_id,
                *(getattr(model, name) == value for name, value in fields.items()),
            )
            .limit(1)
        )
        is not None
    )


async def _prune_crm(session, business_id, lead_ids, contact_ids, customer_ids):
    # Only prune the sender's now-unreferenced records. A shared phone/customer,
    # another channel's enquiry, or a manually created order must survive.
    for lead_id in lead_ids:
        lead = await session.get(Lead, lead_id)
        if lead is None or lead.business_id != business_id:
            continue
        contact_ids.add(lead.contact_id)
        if lead.customer_id:
            customer_ids.add(lead.customer_id)
        referenced = False
        for model in (LeadSource, Enquiry, Appointment, Order):
            referenced |= await _has(session, model, business_id, lead_id=lead_id)
        if not referenced:
            await session.delete(lead)
    await session.flush()
    customer_ids.update(
        await session.scalars(
            select(Customer.id).where(
                Customer.business_id == business_id,
                Customer.contact_id.in_(contact_ids),
            )
        )
    )
    for customer_id in customer_ids:
        customer = await session.get(Customer, customer_id)
        if customer is None or customer.business_id != business_id:
            continue
        contact_ids.add(customer.contact_id)
        referenced = await _has(
            session, SocialIdentity, business_id, contact_id=customer.contact_id
        )
        for model in (Lead, Appointment, Order):
            referenced |= await _has(session, model, business_id, customer_id=customer_id)
        if not referenced:
            await session.delete(customer)
    await session.flush()
    for contact_id in contact_ids:
        referenced = False
        for model in (SocialIdentity, Lead, Customer):
            referenced |= await _has(session, model, business_id, contact_id=contact_id)
        if not referenced:
            await session.execute(
                delete(Contact).where(
                    Contact.business_id == business_id,
                    Contact.id == contact_id,
                )
            )


async def clear_sender(db, tenant, sender):
    """Atomically reset one sender. Caller must hold sender_lock through confirmation.

    Webhook dedup records intentionally survive: deleting them would let Meta
    retries replay old messages or repeat a reset after new demo data is created.
    """
    async with db.transaction(tenant) as session:
        conversation_ids = await _ids(
            session,
            Conversation,
            Conversation.tenant_id == tenant,
            Conversation.channel == "instagram",
            Conversation.external_id == sender,
        )
        request_ids = set(
            await session.scalars(
                select(AiUsageRecord.request_id).where(
                    AiUsageRecord.tenant_id == tenant,
                    AiUsageRecord.channel == "instagram",
                    AiUsageRecord.conversation_id.in_(conversation_ids),
                )
            )
        )
        request_ids.update(
            await session.scalars(
                select(WebhookEvent.request_id).where(
                    WebhookEvent.tenant_id == tenant,
                    WebhookEvent.channel == "instagram",
                    WebhookEvent.external_user_id == sender,
                    WebhookEvent.request_id.is_not(None),
                )
            )
        )
        tickets = list(
            await session.scalars(
                select(SupportTicket).where(
                    SupportTicket.tenant_id == tenant,
                    SupportTicket.channel == "instagram",
                    SupportTicket.external_user_id == sender,
                )
            )
        )
        request_ids.update(ticket.request_id for ticket in tickets)
        business_id = await session.scalar(select(Business.id).where(Business.slug == tenant))
        if business_id is not None:
            lead_ids = set(
                await session.scalars(
                    select(LeadSource.lead_id).where(
                        LeadSource.business_id == business_id,
                        LeadSource.channel == "instagram",
                        LeadSource.external_id == sender,
                    )
                )
            )
            contact_ids = set(
                await session.scalars(
                    select(SocialIdentity.contact_id).where(
                        SocialIdentity.business_id == business_id,
                        SocialIdentity.platform == "instagram",
                        SocialIdentity.external_id == sender,
                    )
                )
            )
            # Before sender attribution existed on webhooks, a failed turn may
            # have only an enquiry. Infer ownership only for an unshared IG lead.
            for lead_id in lead_ids:
                other_sender = await session.scalar(
                    select(LeadSource.id)
                    .where(
                        LeadSource.business_id == business_id,
                        LeadSource.lead_id == lead_id,
                        LeadSource.channel == "instagram",
                        LeadSource.external_id != sender,
                    )
                    .limit(1)
                )
                if other_sender is None:
                    request_ids.update(
                        await session.scalars(
                            select(Enquiry.request_id).where(
                                Enquiry.business_id == business_id,
                                Enquiry.lead_id == lead_id,
                                Enquiry.channel == "instagram",
                            )
                        )
                    )
            booking_keys = {uuid5(key, f"appointment:instagram:{sender}") for key in request_ids}
            appointments = list(
                await session.scalars(
                    select(Appointment).where(
                        Appointment.business_id == business_id,
                        Appointment.request_id.in_(booking_keys),
                    )
                )
            )
            customer_ids = {row.customer_id for row in appointments}
            lead_ids.update(row.lead_id for row in appointments if row.lead_id)
            notification_ids = {
                uuid5(NAMESPACE_URL, f"nivaso:notify:{business_id}:{kind}:{row.id}")
                for kind, rows in (("support_ticket", tickets), ("appointment", appointments))
                for row in rows
            }
            owned_notifications = select(Notification.id).where(
                Notification.business_id == business_id,
                Notification.id.in_(notification_ids),
            )
            await session.execute(
                delete(PushDelivery).where(
                    PushDelivery.notification_id.in_(owned_notifications),
                )
            )
            await session.execute(
                delete(Notification).where(
                    Notification.business_id == business_id,
                    Notification.id.in_(notification_ids),
                )
            )
            await session.execute(
                delete(Appointment).where(
                    Appointment.business_id == business_id,
                    Appointment.id.in_([row.id for row in appointments]),
                )
            )
            await session.execute(
                delete(Enquiry).where(
                    Enquiry.business_id == business_id,
                    Enquiry.channel == "instagram",
                    Enquiry.request_id.in_(request_ids),
                )
            )
            await session.execute(
                delete(LeadSource).where(
                    LeadSource.business_id == business_id,
                    LeadSource.channel == "instagram",
                    LeadSource.external_id == sender,
                )
            )
            await session.execute(
                delete(SocialIdentity).where(
                    SocialIdentity.business_id == business_id,
                    SocialIdentity.platform == "instagram",
                    SocialIdentity.external_id == sender,
                )
            )
            await _prune_crm(session, business_id, lead_ids, contact_ids, customer_ids)
        await session.execute(
            delete(SupportTicket).where(
                SupportTicket.tenant_id == tenant,
                SupportTicket.channel == "instagram",
                SupportTicket.external_user_id == sender,
            )
        )
        await session.execute(
            delete(AiUsageRecord).where(
                AiUsageRecord.tenant_id == tenant,
                AiUsageRecord.channel == "instagram",
                or_(
                    AiUsageRecord.conversation_id.in_(conversation_ids),
                    AiUsageRecord.request_id.in_(request_ids),
                ),
            )
        )
        await session.execute(
            delete(Message).where(
                Message.tenant_id == tenant,
                Message.conversation_id.in_(conversation_ids),
            )
        )
        await session.execute(
            delete(Conversation).where(
                Conversation.tenant_id == tenant,
                Conversation.id.in_(conversation_ids),
            )
        )
        row = await session.get(InstagramDemoReset, (tenant, sender))
        if row is None:
            row = InstagramDemoReset(tenant_id=tenant, external_user_id=sender)
            session.add(row)
        row.cleared_at = datetime.now(UTC)
