import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from context_agent.api import create_app
from context_agent.channels import ADAPTERS
from context_agent.db import ChannelAccount, WebhookEvent


def sign(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def wa_payload(phone_id, wa_id, text, mid):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_id},
                            "messages": [
                                {"from": wa_id, "id": mid, "type": "text", "text": {"body": text}}
                            ],
                        }
                    }
                ]
            }
        ]
    }


# -- pure adapters -------------------------------------------------------


def test_whatsapp_parse_extracts_text_and_ignores_non_text():
    raw = json.dumps(wa_payload("PH", "9199", "hi there", "wamid.1")).encode()
    msgs = ADAPTERS["whatsapp"].parse(raw)
    assert len(msgs) == 1
    assert (msgs[0].external_user_id, msgs[0].text, msgs[0].message_id) == ("9199", "hi there", "wamid.1")
    status_only = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}).encode()
    assert ADAPTERS["whatsapp"].parse(status_only) == []


def test_whatsapp_signature_valid_and_invalid():
    raw = b'{"entry":[]}'
    cfg = {"app_secret": "s3cr3t"}
    good = SimpleNamespace(get=lambda k, d=None: {"x-hub-signature-256": sign("s3cr3t", raw)}.get(k, d))
    bad = SimpleNamespace(get=lambda k, d=None: {"x-hub-signature-256": "sha256=deadbeef"}.get(k, d))
    assert ADAPTERS["whatsapp"].verify(good, raw, cfg) is True
    assert ADAPTERS["whatsapp"].verify(bad, raw, cfg) is False
    # No app_secret configured -> refuse rather than trust.
    assert ADAPTERS["whatsapp"].verify(good, raw, {}) is False


def test_meta_challenge_matches_verify_token():
    a = ADAPTERS["whatsapp"]
    cfg = {"verify_token": "vt"}
    assert a.challenge({"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "C"}, cfg) == "C"
    assert a.challenge({"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "C"}, cfg) is None


def test_telegram_parse_and_secret_verify():
    raw = json.dumps({"message": {"chat": {"id": 55}, "message_id": 7, "text": "hello"}}).encode()
    msgs = ADAPTERS["telegram"].parse(raw)
    assert (msgs[0].external_user_id, msgs[0].text, msgs[0].message_id) == ("55", "hello", "7")
    hdr_ok = SimpleNamespace(get=lambda k, d=None: {"x-telegram-bot-api-secret-token": "tok"}.get(k, d))
    hdr_bad = SimpleNamespace(get=lambda k, d=None: {}.get(k, d))
    assert ADAPTERS["telegram"].verify(hdr_ok, raw, {"webhook_secret": "tok"}) is True
    assert ADAPTERS["telegram"].verify(hdr_bad, raw, {"webhook_secret": "tok"}) is False


# -- end to end (ASGI, in-process) --------------------------------------


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def run(self, tenant, request):
        self.calls.append((tenant, request))
        return {
            "request_id": request.request_id,
            "outcome": "answered",
            "answer": f"echo:{request.message}",
            "tool_results": [],
            "knowledge_units": [],
        }


def app_for(db, agent):
    services = {"db": db, "agent": agent, "settings": SimpleNamespace(request_timeout=10)}
    app = create_app(services)
    app.state.services = services  # ASGITransport does not run the lifespan
    return app


async def seed_account(db, tenant, channel, account_id, config):
    async with db.transaction(tenant) as session:
        session.add(
            ChannelAccount(
                id=uuid4(), tenant_id=tenant, channel=channel, account_id=account_id, config=config
            )
        )


async def test_whatsapp_inbound_runs_agent_sends_reply_and_dedups(db, monkeypatch):
    sent = []

    async def fake_send(config, to, text):
        sent.append((config.get("phone_number_id"), to, text))

    monkeypatch.setattr(ADAPTERS["whatsapp"], "send", fake_send)

    await seed_account(
        db,
        "acme",
        "whatsapp",
        "PHONE1",
        {"app_secret": "sec", "access_token": "tok", "phone_number_id": "PHONE1"},
    )
    agent = FakeAgent()
    app = app_for(db, agent)

    raw = json.dumps(wa_payload("PHONE1", "USER1", "what time do you close?", "wamid.42")).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sign("sec", raw)}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        first = await client.post("/webhooks/whatsapp", content=raw, headers=headers)
        second = await client.post("/webhooks/whatsapp", content=raw, headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    # Agent ran once; the redelivered wamid was deduped.
    assert len(agent.calls) == 1
    tenant, req = agent.calls[0]
    assert tenant == "acme"
    assert req.channel == "whatsapp" and req.external_user_id == "USER1"
    assert req.message == "what time do you close?"
    assert sent == [("PHONE1", "USER1", "echo:what time do you close?")]

    async with db.transaction() as s:
        events = await s.scalar(select(func.count()).select_from(WebhookEvent))
    assert events == 1


async def test_inbound_rejects_bad_signature(db, monkeypatch):
    monkeypatch.setattr(ADAPTERS["whatsapp"], "send", lambda *a, **k: None)
    await seed_account(db, "acme", "whatsapp", "PHONE1", {"app_secret": "sec"})
    agent = FakeAgent()
    app = app_for(db, agent)
    raw = json.dumps(wa_payload("PHONE1", "U", "hi", "wamid.1")).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/webhooks/whatsapp",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
        )
    assert r.status_code == 401
    assert agent.calls == []


async def test_unknown_account_is_acked_and_dropped(db):
    agent = FakeAgent()
    app = app_for(db, agent)
    raw = json.dumps(wa_payload("UNKNOWN", "U", "hi", "wamid.1")).encode()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/webhooks/whatsapp",
            content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=x"},
        )
    assert r.status_code == 200 and agent.calls == []


async def test_get_verify_challenge(db):
    await seed_account(db, "acme", "whatsapp", "PHONE1", {"verify_token": "vt", "app_secret": "s"})
    app = app_for(db, FakeAgent())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        ok = await client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "CHAL-1"},
        )
        bad = await client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "CHAL-1"},
        )
    assert ok.status_code == 200 and ok.text == "CHAL-1"
    assert bad.status_code == 403


async def test_get_verify_rejects_when_no_token_configured(db):
    # An account with no verify_token must not echo a challenge to a token-less request
    # (absent stored token must never match an absent request token).
    await seed_account(db, "acme", "whatsapp", "PHONE1", {"app_secret": "s"})
    app = app_for(db, FakeAgent())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get(
            "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.challenge": "PWN"}
        )
    assert r.status_code == 403
