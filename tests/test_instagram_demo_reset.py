import asyncio
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, select, text
from test_appointment_agent_tools import booking, context
from test_crm_transactions import appointment_body, call, order_body, product, service
from test_crm_transactions import crm as crm_fixture
from test_instagram_integration import CREDS, event
from test_webhooks import app_for, seed_account, sign

from appointments.models import Appointment
from context_agent import conversations, demo_reset, support
from context_agent.channels import ADAPTERS
from context_agent.db import (
    AiUsageRecord,
    Conversation,
    Database,
    InstagramDemoReset,
    Message,
    SupportTicket,
    WebhookEvent,
)
from context_agent.notification_models import Notification, PushDelivery, PushSubscription
from context_agent.schemas import ChatInput
from context_agent.tools import BOOK_APPOINTMENT, execute
from context_agent.webhooks import prepare_webhook, process_jobs, process_webhook
from crm.models import Contact, Customer, Enquiry, Lead, LeadSource, SocialIdentity
from crm.schemas import ContactInput, EnquiryInput, SocialInput
from crm.service import capture_incoming, save_enquiry
from orders.models import Order
from super_admin.businesses.models import BusinessAdmin

crm = crm_fixture


def raw(message="/clear", sender="sender-1", mid=None):
    return json.dumps(
        {
            "entry": [
                event(
                    CREDS["account_id"],
                    sender=sender,
                    mid=mid or str(uuid4()),
                    message=message,
                )
            ]
        }
    ).encode()


class RecordingAgent:
    def __init__(self, db):
        self.db, self.calls, self.histories = db, [], []

    async def run(self, tenant, payload, **kwargs):
        self.calls.append(payload)
        await capture_incoming(self.db, tenant, payload)
        async with self.db.transaction(tenant) as session:
            cid = await conversations.get_or_create(
                session, tenant, payload.channel, payload.external_user_id
            )
            self.histories.append(await conversations.load_history(session, tenant, cid, 10))
            await conversations.record_message(session, tenant, cid, "user", payload.message)
            await conversations.record_message(session, tenant, cid, "assistant", "reply")
        return {"answer": "reply", "conversation_id": cid}


def services_for(db, enabled=True):
    return {
        "db": db,
        "agent": RecordingAgent(db),
        "settings": SimpleNamespace(
            instagram_demo_clear_accounts={CREDS["account_id"]} if enabled else set(),
        ),
    }


async def rows(db, model):
    async with db.transaction() as session:
        return list(await session.scalars(select(model)))


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def send(config, recipient, answer):
        messages.append((recipient, answer))

    monkeypatch.setattr(ADAPTERS["instagram"], "send", send)
    return messages


async def test_clear_bypasses_ai_and_next_turn_is_fresh_even_after_meta_retries(db, sent):
    services = services_for(db)
    first, command = raw("old conversation"), raw(" /CLEAR ")
    await process_webhook(services, "instagram", "demo", CREDS, first)
    old_id = (await rows(db, Conversation))[0].id
    await process_webhook(services, "instagram", "demo", CREDS, command)
    assert not await rows(db, Conversation)
    assert not await rows(db, Message)
    assert len(services["agent"].calls) == 1
    assert sent[-1] == ("sender-1", demo_reset.CONFIRMATION)
    await process_webhook(services, "instagram", "demo", CREDS, raw("new scenario"))
    fresh = (await rows(db, Conversation))[0]
    assert fresh.id != old_id
    assert services["agent"].histories == [[], []]
    for duplicate in (first, command):
        await process_webhook(services, "instagram", "demo", CREDS, duplicate)
    assert len(sent) == 3
    assert (await rows(db, Conversation))[0].id == fresh.id
    assert [m.content for m in await rows(db, Message)] == ["new scenario", "reply"]
    assert len(await rows(db, WebhookEvent)) == 3


@pytest.mark.parametrize(
    "message", ["clear", "please /clear", "/clear everything", "/clear\nhello"]
)
async def test_only_standalone_command_resets(db, sent, message):
    services = services_for(db)
    await process_webhook(services, "instagram", "demo", CREDS, raw(message))
    assert len(services["agent"].calls) == 1
    assert not await rows(db, InstagramDemoReset)


async def test_disabled_and_wrong_account_do_not_clear(db, sent):
    services = services_for(db, enabled=False)
    await process_webhook(services, "instagram", "demo", CREDS, raw())
    services["settings"].instagram_demo_clear_accounts = {"different-account"}
    await process_webhook(services, "instagram", "demo", CREDS, raw())
    assert len(services["agent"].calls) == 2
    assert len(await rows(db, Message)) == 4
    assert not await rows(db, InstagramDemoReset)
    assert not demo_reset.enabled(services["settings"], "whatsapp", CREDS)


async def test_invalid_signature_cannot_reset(db, sent):
    agent = RecordingAgent(db)
    app = app_for(db, agent)
    app.state.services["settings"].instagram_demo_clear_accounts = {CREDS["account_id"]}
    await seed_account(db, "demo", "instagram", CREDS["account_id"], CREDS)
    payload = raw()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/instagram",
            content=payload,
            headers={
                "x-hub-signature-256": sign("wrong-secret", payload),
            },
        )
        assert response.status_code == 401
        assert not await rows(db, InstagramDemoReset)
        response = await client.post(
            "/webhooks/instagram",
            content=payload,
            headers={
                "x-hub-signature-256": sign(CREDS["app_secret"], payload),
            },
        )
        assert response.status_code == 200
    assert len(await rows(db, InstagramDemoReset)) == 1
    assert agent.calls == []


async def seed_demo(c, sender="sender-1", owner=None, channel="instagram"):
    business = (owner or c.a).business
    request = ChatInput(
        message="Book a consultation",
        request_id=uuid4(),
        channel=channel,
        external_user_id=sender,
    )
    await capture_incoming(c.db, business.slug, request)
    async with c.db.transaction(business.slug) as session:
        cid = await conversations.get_or_create(session, business.slug, channel, sender)
        await conversations.record_message(session, business.slug, cid, "user", request.message)
        await support.create_or_get(
            session,
            business.slug,
            request.request_id,
            "Question",
            "Demo ticket",
            channel=channel,
            external_user_id=sender,
        )
        session.add(
            AiUsageRecord(
                tenant_id=business.slug,
                channel=channel,
                conversation_id=cid,
                request_id=request.request_id,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                duration_ms=1,
                input_tokens=1,
                output_tokens=1,
                tokens_complete=True,
                llm_calls=1,
                status="completed",
            )
        )
    return request


async def test_clears_linked_records_with_foreign_keys_and_preserves_other_scopes(crm):
    c = crm
    async with c.db.engine.begin() as connection:
        await connection.execute(text("PRAGMA foreign_keys=ON"))
    # Keep a real FK-linked push outbox record in the deletion graph.
    async with c.db.transaction() as session:
        admin = await session.scalar(
            select(BusinessAdmin).where(
                BusinessAdmin.business_id == c.a.business.id,
            )
        )
        session.add(
            PushSubscription(
                business_id=c.a.business.id,
                admin_id=admin.id,
                token_version=admin.token_version,
                endpoint_hash="demo-device",
                endpoint="https://example.invalid/push",
                p256dh="test",
                auth="test",
            )
        )
    request = await seed_demo(c)
    await seed_demo(c, "another-sender")
    await seed_demo(c, owner=c.b)
    await seed_demo(c, channel="whatsapp")
    s = await service(c)
    ctx = context(
        c, channel="instagram", external_user_id="sender-1", request_id=request.request_id
    )
    booked = await execute(BOOK_APPOINTMENT, booking(s), ctx)
    assert booked["ok"] is True
    old_lead = next(row for row in await rows(c.db, Lead) if row.customer_id)
    keep = {}
    for model in (Conversation, Message, SupportTicket, AiUsageRecord):
        keep[model] = {
            row.id
            for row in await rows(c.db, model)
            if (
                row.tenant_id != c.a.business.slug
                or (getattr(row, "channel", "instagram") != "instagram")
                or (
                    getattr(row, "external_id", getattr(row, "external_user_id", ""))
                    == "another-sender"
                )
            )
        }
    await demo_reset.clear_sender(c.db, c.a.business.slug, "sender-1")
    for model in (Conversation, SupportTicket, AiUsageRecord):
        assert len(await rows(c.db, model)) == 3
        assert keep[model] <= {row.id for row in await rows(c.db, model)}
    assert len(await rows(c.db, Message)) == 3
    assert not await rows(c.db, Appointment)
    assert not await rows(c.db, Customer)
    assert len(await rows(c.db, Enquiry)) == 3
    assert len(await rows(c.db, Lead)) == 3
    assert len(await rows(c.db, Contact)) == 3
    assert len(await rows(c.db, SocialIdentity)) == 3
    assert len(await rows(c.db, LeadSource)) == 3
    assert len(await rows(c.db, Notification)) == 3
    assert len(await rows(c.db, PushDelivery)) == 2
    assert len(await rows(c.db, PushSubscription)) == 1
    # Reusing the demo phone and sender produces a new lead and appointment.
    fresh = await seed_demo(c)
    ctx = context(c, channel="instagram", external_user_id="sender-1", request_id=fresh.request_id)
    again = await execute(BOOK_APPOINTMENT, booking(s), ctx)
    assert again["ok"] is True and again["appointment_id"] != booked["appointment_id"]
    assert next(row for row in await rows(c.db, Lead) if row.customer_id).id != old_lead.id


async def test_shared_contact_other_instagram_identity_and_manual_order_survive(crm):
    c = crm
    first = await seed_demo(c)
    s = await service(c)
    ctx = context(c, channel="instagram", external_user_id="sender-1", request_id=first.request_id)
    assert (await execute(BOOK_APPOINTMENT, booking(s), ctx))["ok"]
    customer = (await rows(c.db, Customer))[0]
    other_request = uuid4()
    async with c.db.transaction(c.a.business.slug) as session:
        await save_enquiry(
            session,
            c.a.business.id,
            EnquiryInput(
                request_id=other_request,
                message="Other person's enquiry",
                channel="instagram",
                contact=ContactInput(
                    phone="+919876543210",
                    social_identities=[
                        SocialInput(platform="instagram", external_id="other-instagram-sender"),
                    ],
                ),
            ),
            source=("instagram", "other-instagram-sender"),
        )
    p = await product(c)
    order = await call(c, "POST", "/orders", order_body(p, customer_id=str(customer.id)), code=201)
    manual_booking = await call(
        c, "POST", "/appointments", appointment_body(s, customer_id=str(customer.id)), code=201
    )
    await demo_reset.clear_sender(c.db, c.a.business.slug, "sender-1")
    assert [str(row.id) for row in await rows(c.db, Appointment)] == [manual_booking["id"]]
    assert [row.id for row in await rows(c.db, Customer)] == [customer.id]
    assert [str(row.id) for row in await rows(c.db, Order)] == [order["id"]]
    assert [row.external_id for row in await rows(c.db, SocialIdentity)] == [
        "other-instagram-sender"
    ]
    assert [row.request_id for row in await rows(c.db, Enquiry)] == [other_request]
    assert len(await rows(c.db, Notification)) == 2


async def test_failure_rolls_back_entire_cleanup_and_never_claims_success(crm, monkeypatch, sent):
    c = crm
    await seed_demo(c)

    async def fail(*args):
        raise RuntimeError("injected failure after deleting notifications")

    monkeypatch.setattr(demo_reset, "_prune_crm", fail)
    services = services_for(c.db)
    await process_webhook(services, "instagram", c.a.business.slug, CREDS, raw())
    for model in (Conversation, Message, Enquiry, Lead, Contact, SupportTicket, Notification):
        assert len(await rows(c.db, model)) == 1
    assert not await rows(c.db, InstagramDemoReset)
    assert "reset failed" in sent[-1][1]
    assert (await rows(c.db, WebhookEvent))[0].error_code == "demo_reset_failed"
    assert services["agent"].calls == []


async def test_inflight_reply_finishes_before_clear_and_queued_old_turn_is_ignored(db, sent):
    services = services_for(db)
    started, release = asyncio.Event(), asyncio.Event()
    original = services["agent"].run

    async def slow_run(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    services["agent"].run = slow_run
    first = await prepare_webhook(services, "instagram", "demo", raw("in flight"))
    clear = await prepare_webhook(services, "instagram", "demo", raw())
    old_queue = await prepare_webhook(services, "instagram", "demo", raw("old queued turn"))
    running = asyncio.create_task(process_jobs(services, "instagram", "demo", CREDS, first))
    await asyncio.wait_for(started.wait(), 2)
    clearing = asyncio.create_task(process_jobs(services, "instagram", "demo", CREDS, clear))
    await asyncio.sleep(0)
    assert not clearing.done()
    release.set()
    await asyncio.gather(running, clearing)
    await process_jobs(services, "instagram", "demo", CREDS, old_queue)
    assert not await rows(db, Message)
    assert [answer for _, answer in sent] == ["reply", demo_reset.CONFIRMATION]
    assert len(services["agent"].calls) == 1
    assert (await rows(db, WebhookEvent))[-1].error_code == "demo_reset_superseded"


async def test_confirmation_send_failure_keeps_reset_committed_and_retry_is_deduped(
    db,
    monkeypatch,
):
    services = services_for(db)
    calls = []

    async def fail_send(*args):
        calls.append(args)
        raise RuntimeError("channel unavailable")

    monkeypatch.setattr(ADAPTERS["instagram"], "send", fail_send)
    await services["agent"].run(
        "demo",
        ChatInput(
            message="before reset",
            request_id=uuid4(),
            channel="instagram",
            external_user_id="sender-1",
        ),
    )
    command = raw()
    await process_webhook(services, "instagram", "demo", CREDS, command)
    assert not await rows(db, Conversation)
    assert len(await rows(db, InstagramDemoReset)) == 1
    assert (await rows(db, WebhookEvent))[0].error_code == "send_failed"
    await process_webhook(services, "instagram", "demo", CREDS, command)
    assert len(calls) == 1


async def test_booking_without_leads_is_attributed_via_webhook_request(crm):
    from modules.models import BusinessModules

    c = crm
    async with c.db.transaction() as session:
        modules = await session.get(BusinessModules, c.a.business.id)
        modules.leads = False
    services = services_for(c.db)
    jobs = await prepare_webhook(services, "instagram", c.a.business.slug, raw("book please"))
    request = jobs[0][1]
    s = await service(c)
    ctx = context(
        c, channel="instagram", external_user_id="sender-1", request_id=request.request_id
    )
    assert (await execute(BOOK_APPOINTMENT, booking(s), ctx))["ok"]
    assert not await rows(c.db, Lead)
    await demo_reset.clear_sender(c.db, c.a.business.slug, "sender-1")
    for model in (Appointment, Customer, Contact, SocialIdentity, Notification):
        assert not await rows(c.db, model)


async def test_postgres_cross_worker_lock_and_reset_cutoff(sent):
    # Optional integration check, against an explicitly created disposable LOCAL DB.
    url = os.getenv("INSTAGRAM_RESET_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set INSTAGRAM_RESET_TEST_DATABASE_URL for PostgreSQL lock integration")
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1"}
    assert parsed.database.startswith("demo_reset_test_")
    first_db, second_db = Database(url), Database(url)
    from context_agent.db import Base

    async with first_db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    services = services_for(first_db)
    other_worker = services_for(second_db)
    tenant, sender = "lock-test-" + uuid4().hex[:8], "sender-1"
    started, release = asyncio.Event(), asyncio.Event()
    original = services["agent"].run

    async def slow_run(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    services["agent"].run = slow_run
    try:
        first = await prepare_webhook(services, "instagram", tenant, raw("in flight"))
        clear = await prepare_webhook(other_worker, "instagram", tenant, raw())
        old_queue = await prepare_webhook(other_worker, "instagram", tenant, raw("queued"))
        async with asyncio.timeout(10):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(process_jobs(services, "instagram", tenant, CREDS, first))
                await started.wait()
                clearing = tasks.create_task(
                    process_jobs(other_worker, "instagram", tenant, CREDS, clear)
                )
                await asyncio.sleep(0.2)
                assert not clearing.done()
                release.set()
        await process_jobs(other_worker, "instagram", tenant, CREDS, old_queue)
        async with second_db.transaction() as session:
            assert (
                await session.scalar(
                    select(Conversation).where(
                        Conversation.tenant_id == tenant,
                        Conversation.external_id == sender,
                    )
                )
                is None
            )
        assert [answer for _, answer in sent] == ["reply", demo_reset.CONFIRMATION]
        assert len(services["agent"].calls) == 1 and other_worker["agent"].calls == []
    finally:
        await first_db.close()
        await second_db.close()


def test_migration_keeps_existing_events_and_downgrades():
    path = Path(__file__).parents[1] / "migrations/versions/021_instagram_demo_reset.py"
    spec = importlib.util.spec_from_file_location("demo_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE webhook_events (id TEXT PRIMARY KEY, tenant_id TEXT, channel TEXT)")
        )
        connection.execute(text("INSERT INTO webhook_events VALUES ('old', 'demo', 'instagram')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.execute(
                    text("SELECT external_user_id FROM webhook_events WHERE id='old'")
                ).scalar_one()
                is None
            )
            assert "instagram_demo_resets" in inspect(connection).get_table_names()
            migration.downgrade()
            assert inspect(connection).get_table_names() == ["webhook_events"]
            assert connection.execute(text("SELECT count(*) FROM webhook_events")).scalar_one() == 1
    engine.dispose()
