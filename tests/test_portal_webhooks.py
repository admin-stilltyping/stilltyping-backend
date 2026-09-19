import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import test_instagram_integration
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from test_instagram_integration import CREDS, configure, deliver, event
from test_webhooks import FakeAgent, app_for, seed_account, sign, wa_payload

from context_agent.channels import ADAPTERS, ChannelNotConfigured
from context_agent.db import WebhookEvent
from context_agent.schemas import DomainError
from context_agent.webhooks import prepare_webhook, process_jobs, process_webhook, send_error_code

integration = test_instagram_integration.integration


async def rows(db):
    async with db.transaction() as session:
        return list(await session.scalars(select(WebhookEvent).order_by(WebhookEvent.created_at)))


async def test_persists_received_before_processing_and_duplicate_does_not_reply_twice(
    db, monkeypatch
):
    agent = FakeAgent()
    sent = []

    async def send(*args):
        sent.append(args)
        assert (await rows(db))[0].status == "processing"

    monkeypatch.setattr(ADAPTERS["whatsapp"], "send", send)
    services = {"db": db, "agent": agent}
    raw = json.dumps(wa_payload("PHONE", "sender", "hi", "mid-1")).encode()
    jobs = await prepare_webhook(services, "whatsapp", "clinic", raw, "account")
    first = (await rows(db))[0]
    assert first.status == "received" and first.tenant_id == "clinic"
    assert first.external_event_id == "mid-1" and first.deliveries == 1
    assert await prepare_webhook(services, "whatsapp", "clinic", raw, "account") == []
    await process_jobs(services, "whatsapp", "clinic", {}, jobs)
    assert await prepare_webhook(services, "whatsapp", "clinic", raw, "account") == []
    record = (await rows(db))[0]
    assert record.status == "processed" and record.deliveries == 3
    assert record.completed_at and record.duration_ms > 0
    assert len(agent.calls) == len(sent) == 1


async def test_telegram_ids_are_scoped_to_business_account_and_chat(db, monkeypatch):
    agent = FakeAgent()
    sent = []

    async def send(*args):
        sent.append(args)

    monkeypatch.setattr(ADAPTERS["telegram"], "send", send)
    services = {"db": db, "agent": agent}
    for tenant, account, sender in [
        ("a", "bot-a", 1),
        ("b", "bot-b", 1),
        ("a", "bot-a", 2),
        ("a", "bot-c", 1),
    ]:
        raw = json.dumps(
            {"message": {"chat": {"id": sender}, "message_id": 7, "text": "hi"}}
        ).encode()
        jobs = await prepare_webhook(services, "telegram", tenant, raw, account)
        await process_jobs(services, "telegram", tenant, {}, jobs)
        assert await prepare_webhook(services, "telegram", tenant, raw, account) == []
    assert len(agent.calls) == len(sent) == 4
    assert len({call[1].request_id for call in agent.calls}) == 4
    assert all(row.deliveries == 2 for row in await rows(db))


async def test_unsupported_and_invalid_messages_are_recorded_without_ai_calls(db):
    agent = FakeAgent()
    services = {"db": db, "agent": agent}
    media = wa_payload("PHONE", "sender", "", "media-1")
    media["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "image"
    for data in [
        media,
        wa_payload("PHONE", "sender", "hi", ""),
        wa_payload("PHONE", "sender", "   ", "blank-1"),
    ]:
        await process_webhook(services, "whatsapp", "clinic", {}, json.dumps(data).encode())
    records = await rows(db)
    assert [row.error_code for row in records] == [
        "unsupported_message",
        "missing_message_identity",
        "invalid_message",
    ]
    assert all(row.status == "ignored" and row.completed_at for row in records)
    assert agent.calls == []


async def test_legacy_duplicate_is_skipped_without_claiming_old_record_ownership(db):
    async with db.transaction() as session:
        session.add(WebhookEvent(channel="instagram", event_id="old-mid"))
    raw = json.dumps({"entry": [event("account", mid="old-mid")]}).encode()
    services = {"db": db, "agent": FakeAgent()}
    await process_webhook(services, "instagram", "clinic", {}, raw)
    old, new = await rows(db)
    assert old.tenant_id is None and old.status == "legacy"
    assert new.tenant_id == "clinic" and new.error_code == "legacy_duplicate"
    assert new.status == "ignored" and services["agent"].calls == []


@pytest.mark.parametrize("stage", ["generate", "send", "credentials"])
async def test_failures_are_saved_without_leaking_exception_secrets(db, monkeypatch, caplog, stage):
    agent = FakeAgent()

    async def fail(*args, **kwargs):
        raise RuntimeError("https://secret-token.example/private-customer-message")

    if stage == "generate":
        agent.run = fail
    elif stage == "send":
        monkeypatch.setattr(ADAPTERS["whatsapp"], "send", fail)
    await process_webhook(
        {"db": db, "agent": agent},
        "whatsapp",
        "clinic",
        {},
        json.dumps(wa_payload("P", "S", "hi", "mid")).encode(),
    )
    record = (await rows(db))[0]
    assert record.status == "failed"
    assert (
        record.error_code
        == {
            "generate": "generation_failed",
            "send": "send_failed",
            "credentials": "channel_not_configured",
        }[stage]
    )
    assert record.duration_ms > 0 and record.completed_at
    assert "secret-token" not in caplog.text and "private-customer" not in caplog.text


@pytest.mark.parametrize("code, text", [
    ("model_rate_limited", "rate or quota limit (HTTP 429)"),
    ("model_unavailable", "temporarily unavailable (HTTP 503)"),
    ("model_authorization_failed", "Gemini rejected access"),
    ("gemini_not_configured", "No Gemini API key is configured"),
    ("secret-token-untrusted-code", "exact cause was not recorded"),
])
async def test_generation_failure_reason_reaches_owner_without_secrets(
    integration, caplog, code, text
):
    c = integration
    agent = FakeAgent()

    async def fail(*args, **kwargs):
        raise DomainError(503, code, "secret-token private-customer-message")

    agent.run = fail
    await process_webhook(
        {"db": c.db, "agent": agent}, "instagram", c.a.business.slug, {},
        json.dumps({"entry": [event(CREDS["account_id"])]}).encode(),
    )
    record = (await rows(c.db))[0]
    assert record.status == "failed" and record.completed_at and record.duration_ms > 0
    assert record.error_code == (
        "generation_failed" if code == "secret-token-untrusted-code" else code
    )
    assert c.sent == []
    url = f"/admin/{c.a.business.slug}/webhook-events"
    response = await c.client.get(url, headers=c.a.headers)
    assert response.status_code == 200
    assert text in response.json()["items"][0]["detail"]
    assert str(record.request_id) in caplog.text
    for secret in ("secret-token", "private-customer-message"):
        assert secret not in response.text and secret not in caplog.text
    assert (await c.client.get(url, headers=c.b.headers)).status_code == 403


@pytest.mark.parametrize("channel", ["instagram", "whatsapp", "telegram"])
async def test_missing_credentials_raise_instead_of_claiming_send_success(channel):
    with pytest.raises(ChannelNotConfigured):
        await ADAPTERS[channel].send({}, "recipient", "reply")


def test_provider_failure_classification_never_returns_raw_response():
    for status, code in [
        (401, "channel_authorization_failed"),
        (403, "channel_authorization_failed"),
        (429, "channel_rate_limited"),
        (500, "send_failed"),
    ]:
        response = httpx.Response(
            status,
            request=httpx.Request("POST", "https://provider/secret"),
            json={"secret": "private"},
        )
        assert (
            send_error_code(
                httpx.HTTPStatusError("secret", request=response.request, response=response)
            )
            == code
        )
    assert send_error_code(httpx.ReadTimeout("secret")) == "send_timeout"


async def test_instagram_portal_tracks_business_events_and_rejects_other_owners(integration):
    c = integration
    await configure(c)
    response = await deliver(c, [event(CREDS["account_id"])])
    assert response.status_code == 200
    await deliver(c, [event(CREDS["account_id"])])
    url = f"/admin/{c.a.business.slug}/webhook-events"
    assert (await c.client.get(url)).status_code == 401
    assert (await c.client.get(url, headers=c.b.headers)).status_code == 403
    response = await c.client.get(url, headers=c.a.headers)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    body = response.json()
    assert body["summary"] == {
        "events": 1,
        "replies_sent": 1,
        "failed": 0,
        "duplicate_deliveries": 1,
    }
    row = body["items"][0]
    assert row["source"] == "instagram" and row["status"] == "processed"
    assert row["external_event_id"] == "ig.mid-1" and row["deliveries"] == 2
    assert row["created_at"].endswith("+00:00")
    for secret in [
        CREDS["access_token"],
        CREDS["app_secret"],
        CREDS["verify_token"],
        "sender-1",
        "Hello",
    ]:
        assert secret not in response.text
    own_b = (
        await c.client.get(f"/admin/{c.b.business.slug}/webhook-events", headers=c.b.headers)
    ).json()
    assert own_b["items"] == [] and own_b["total_count"] == 0


async def test_filters_pagination_and_business_timezone_hide_legacy_rows(integration):
    c = integration
    async with c.db.transaction() as session:
        for tenant, when, channel, status in [
            (None, datetime(2026, 9, 18, 12, tzinfo=UTC), "instagram", "legacy"),
            (
                c.a.business.slug,
                datetime(2026, 9, 17, 18, 29, tzinfo=UTC),
                "instagram",
                "processed",
            ),
            (
                c.a.business.slug,
                datetime(2026, 9, 17, 18, 30, tzinfo=UTC),
                "instagram",
                "processed",
            ),
            (c.a.business.slug, datetime(2026, 9, 18, 12, tzinfo=UTC), "whatsapp", "failed"),
            (
                c.a.business.slug,
                datetime(2026, 9, 18, 18, 30, tzinfo=UTC),
                "instagram",
                "processed",
            ),
        ]:
            session.add(
                WebhookEvent(
                    tenant_id=tenant,
                    channel=channel,
                    event_id=str(uuid4()),
                    created_at=when,
                    status=status,
                    deliveries=1,
                )
            )
    url = f"/admin/{c.a.business.slug}/webhook-events"
    params = {"start": "2026-09-18", "end": "2026-09-18", "limit": 1}
    data = (await c.client.get(url, params=params, headers=c.a.headers)).json()
    assert data["total_count"] == 2 and data["next_offset"] == 1
    assert data["items"][0]["status"] == "failed"
    second = (await c.client.get(url, params={**params, "offset": 1}, headers=c.a.headers)).json()
    assert second["next_offset"] is None and second["items"][0]["id"] != data["items"][0]["id"]
    for filters in ({"source": "whatsapp"}, {"status": "failed"}):
        filtered = (
            await c.client.get(url, params={**params, **filters}, headers=c.a.headers)
        ).json()
        assert filtered["total_count"] == 1 and filtered["summary"]["failed"] == 1
    for invalid in (
        {"start": "2026-09-19", "end": "2026-09-18"},
        {"source": "razorpay"},
        {"status": "arbitrary"},
        {"offset": -1},
        {"limit": 101},
    ):
        assert (await c.client.get(url, params=invalid, headers=c.a.headers)).status_code == 400


async def test_unverified_events_and_echoes_are_not_logged(integration):
    c = integration
    await configure(c)
    assert (await deliver(c, [event(CREDS["account_id"])], secret="wrong")).status_code == 401
    echo = event(CREDS["account_id"])
    echo["messaging"][0]["message"]["is_echo"] = True
    await deliver(c, [echo])
    await deliver(c, [{"id": CREDS["account_id"], "messaging": [{"read": {"watermark": 123}}]}])
    assert await rows(c.db) == [] and c.calls == []


async def test_whatsapp_batches_do_not_mix_business_messages(db, monkeypatch):
    sent = []

    async def send(config, recipient, message):
        sent.append((config["phone_number_id"], recipient, message))

    monkeypatch.setattr(ADAPTERS["whatsapp"], "send", send)
    for tenant, phone in [("clinic-a", "PHONE-A"), ("clinic-b", "PHONE-B")]:
        await seed_account(
            db, tenant, "whatsapp", phone, {"phone_number_id": phone, "app_secret": "shared"}
        )
    agent = FakeAgent()
    raw = json.dumps(
        {
            "entry": wa_payload("PHONE-A", "USER-A", "hi a", "a")["entry"]
            + wa_payload("PHONE-B", "USER-B", "hi b", "b")["entry"]
        }
    ).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(db, agent)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sign("shared", raw)}
        )
    assert response.status_code == 200
    assert [(tenant, payload.message) for tenant, payload in agent.calls] == [
        ("clinic-a", "hi a"),
        ("clinic-b", "hi b"),
    ]
    assert sent == [("PHONE-A", "USER-A", "echo:hi a"), ("PHONE-B", "USER-B", "echo:hi b")]
    assert {row.tenant_id for row in await rows(db)} == {"clinic-a", "clinic-b"}


def test_migration_preserves_old_dedup_rows_and_matches_metadata():
    spec = importlib.util.spec_from_file_location(
        "webhook_log_migration",
        Path(__file__).parents[1] / "migrations/versions/015_webhook_event_log.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE webhook_events (id CHAR(32) PRIMARY KEY, channel VARCHAR(20), event_id VARCHAR(200), created_at DATETIME, UNIQUE(channel,event_id))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO webhook_events VALUES ('old', 'instagram', 'old-id', CURRENT_TIMESTAMP)"
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert {
                    column["name"] for column in inspect(connection).get_columns("webhook_events")
                } == set(WebhookEvent.__table__.columns.keys())
                old = connection.execute(
                    text("SELECT tenant_id, status, deliveries FROM webhook_events")
                ).one()
                assert tuple(old) == (None, "legacy", 1)
                migration.downgrade()
                assert (
                    connection.execute(text("SELECT event_id FROM webhook_events")).scalar_one()
                    == "old-id"
                )
    finally:
        engine.dispose()
