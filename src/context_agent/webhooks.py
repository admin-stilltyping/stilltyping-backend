"""Inbound social-channel webhooks (WhatsApp, Instagram, Telegram).

Flow per delivery:
  1. Resolve the ChannelAccount from the platform's routing key (unverified read).
  2. Verify the signature / secret against that account's stored credentials.
  3. Return 200 immediately; process in the background so the platform never
     times out and redelivers.
  4. Dedup on the platform message id (webhook_events), then run the same agent
     conversation loop as the API, and send the reply back on the same channel.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import BackgroundTasks, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import select

from super_admin.businesses.models import Business

from .channels import ADAPTERS, ChannelNotConfigured
from .db import AiUsageRecord, ChannelAccount, WebhookEvent
from .schemas import ChatInput, DomainError

log = logging.getLogger(__name__)


async def _accounts_for_channel(session, channel):
    return list(
        (
            await session.scalars(select(ChannelAccount).where(ChannelAccount.channel == channel))
        ).all()
    )


async def _lookup_account(session, channel, account_id):
    if not account_id:
        return None
    return await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.channel == channel, ChannelAccount.account_id == account_id
        )
    )


async def prepare_webhook(services, channel, tenant, raw, account_ref=""):
    """Persist verified arrivals before acknowledging the provider; claim each once."""
    jobs = []
    async with services["db"].transaction(tenant) as session:
        for message in ADAPTERS[channel].events(raw):
            # Provider IDs can be unique only within one account/chat (Telegram).
            # The key contains no stored credentials or customer message content.
            identity = [
                tenant,
                channel,
                account_ref,
                message.external_user_id,
                message.message_id or hashlib.sha256(raw).hexdigest(),
            ]
            key = "v2:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            now = datetime.now(UTC)
            row = await session.scalar(
                select(WebhookEvent).where(
                    WebhookEvent.channel == channel,
                    WebhookEvent.event_id == key,
                )
            )
            if row is not None:
                row.deliveries += 1
                row.last_received_at = now
                continue
            # Retain the old at-most-once guarantee for messages seen before tracking.
            legacy = message.message_id and await session.scalar(
                select(WebhookEvent.id).where(
                    WebhookEvent.channel == channel,
                    WebhookEvent.event_id == message.message_id,
                    WebhookEvent.tenant_id.is_(None),
                )
            )
            reason = "legacy_duplicate" if legacy else message.ignored_reason
            if not reason and (not message.message_id or not message.external_user_id):
                reason = "missing_message_identity"
            request_id = uuid5(NAMESPACE_URL, f"context-agent:webhook:{key}")
            payload = None
            if not reason:
                try:
                    payload = ChatInput(
                        message=message.text,
                        request_id=request_id,
                        channel=channel,
                        external_user_id=message.external_user_id,
                    )
                except ValidationError:
                    reason = "invalid_message"
            row = WebhookEvent(
                channel=channel,
                event_id=key,
                tenant_id=tenant,
                external_event_id=message.message_id[:512] or None,
                status="ignored" if reason else "received",
                deliveries=1,
                created_at=now,
                last_received_at=now,
                completed_at=now if reason else None,
                error_code=reason,
                request_id=request_id,
            )
            session.add(row)
            await session.flush()
            if payload is not None:
                jobs.append((row.id, payload))
    return jobs


async def update_event(db, tenant, event_id, status, *, started=None, error_code=None):
    async with db.transaction(tenant) as session:
        row = await session.get(WebhookEvent, event_id)
        if row is not None and row.tenant_id == tenant:
            row.status = status
            row.error_code = error_code
            if started is not None:
                row.completed_at = datetime.now(UTC)
                row.duration_ms = round((perf_counter() - started) * 1000, 2)


def send_error_code(exc):
    if isinstance(exc, ChannelNotConfigured):
        return "channel_not_configured"
    if isinstance(exc, httpx.TimeoutException):
        return "send_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "channel_authorization_failed"
        if status == 429:
            return "channel_rate_limited"
    return "send_failed"


def generation_error_code(exc):
    # Only trusted application codes are safe to persist or show to a business owner.
    if isinstance(exc, DomainError) and exc.code in {
        "model_rate_limited",
        "model_unavailable",
        "model_authorization_failed",
        "gemini_not_configured",
    }:
        return exc.code
    return "generation_failed"


async def process_jobs(services, channel, tenant, config, jobs):
    adapter = ADAPTERS[channel]
    db, agent = services["db"], services["agent"]
    for event_id, payload in jobs:
        started = perf_counter()
        await update_event(db, tenant, event_id, "processing")
        try:
            result = await agent.run(tenant, payload, await_delivery=True)
        except Exception as exc:
            # Exceptions from SDKs can contain request URLs or tokens. Persist only
            # a safe reason, never exception strings or the raw provider payload.
            error_code = generation_error_code(exc)
            log.error(
                "webhook_agent_failed channel=%s event=%s request_id=%s code=%s",
                channel, event_id, payload.request_id, error_code,
            )
            await update_event(
                db, tenant, event_id, "failed", started=started, error_code=error_code
            )
            continue
        error_code = None
        try:
            await adapter.send(config, payload.external_user_id, result["answer"])
        except Exception as exc:
            error_code = send_error_code(exc)
            log.error(
                "webhook_send_failed channel=%s event=%s code=%s", channel, event_id, error_code
            )
        await finish_delivery(db, tenant, result.get("usage_id"), started, error_code is None)
        await update_event(
            db,
            tenant,
            event_id,
            "failed" if error_code else "processed",
            started=started,
            error_code=error_code,
        )


async def process_webhook(services, channel, tenant, config, raw):
    """Convenience entry point for a verified delivery, also used by tests."""
    jobs = await prepare_webhook(services, channel, tenant, raw)
    await process_jobs(services, channel, tenant, config, jobs)


async def finish_delivery(db, tenant, usage_id, started, sent):
    if usage_id is None:
        return
    try:
        async with db.transaction() as session:
            row = await session.get(AiUsageRecord, usage_id)
            if row is not None and row.tenant_id == tenant:
                if row.status == "awaiting_send":
                    row.status = "completed" if sent else "send_failed"
                row.completed_at = datetime.now(UTC)
                row.duration_ms = round((perf_counter() - started) * 1000, 2)
    except Exception as exc:
        log.error("ai_usage_delivery_save_failed tenant=%s error=%s", tenant, type(exc).__name__)


def register_webhooks(app):
    @app.get("/webhooks/{channel}")
    async def verify(channel: str, request: Request):
        adapter = ADAPTERS.get(channel)
        if adapter is None or not adapter.has_challenge:
            return Response(status_code=404)
        params = dict(request.query_params)
        token = params.get("hub.verify_token")
        async with request.app.state.services["db"].transaction() as session:
            accounts = await _accounts_for_channel(session, channel)
        for account in accounts:
            expected = account.config.get(adapter.verify_field)
            if expected and expected == token:
                challenge = adapter.challenge(params, account.config)
                if challenge is not None:
                    return PlainTextResponse(challenge)
        log.warning("webhook_verify_rejected channel=%s", channel)
        return Response(status_code=403)

    @app.post("/webhooks/{channel}")
    async def inbound(channel: str, request: Request, background_tasks: BackgroundTasks):
        adapter = ADAPTERS.get(channel)
        if adapter is None:
            return Response(status_code=404)
        raw = await request.body()
        services = request.app.state.services
        if channel in {"instagram", "whatsapp"}:
            # Resolve and verify every account against the original signed body.
            # Each background job receives only that account's messages.
            try:
                body = json.loads(raw)
                entries = body.get("entry", []) if isinstance(body, dict) else []
                if not isinstance(entries, list):
                    return Response(status_code=400)
            except (ValueError, UnicodeError):
                return Response(status_code=400)
            parts = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if channel == "instagram":
                    parts.append((str(entry.get("id") or ""), {**body, "entry": [entry]}))
                else:
                    for change in entry.get("changes", []):
                        value = change.get("value", {})
                        key = value.get("metadata", {}).get("phone_number_id")
                        parts.append((key, {**body, "entry": [{**entry, "changes": [change]}]}))
            deliveries = []
            async with services["db"].transaction() as session:
                for key, part in parts:
                    account = await _lookup_account(session, channel, key)
                    if account is None:
                        continue
                    if not adapter.verify(request.headers, raw, account.config):
                        log.warning("webhook_signature_invalid channel=%s", channel)
                        return Response(status_code=401)
                    business = await session.scalar(
                        select(Business).where(Business.slug == account.tenant_id)
                    )
                    if business is not None and business.status != "active":
                        continue
                    deliveries.append(
                        (
                            account.tenant_id,
                            str(account.id),
                            dict(account.config),
                            json.dumps(part).encode(),
                        )
                    )
            for tenant, account_ref, config, part_raw in deliveries:
                jobs = await prepare_webhook(services, channel, tenant, part_raw, account_ref)
                if jobs:
                    background_tasks.add_task(process_jobs, services, channel, tenant, config, jobs)
            return Response(status_code=200)
        key = adapter.routing_key(request.headers, raw)
        async with services["db"].transaction() as session:
            account = await _lookup_account(session, channel, key)
            if account is None:
                return Response(status_code=200)
            if not adapter.verify(request.headers, raw, account.config):
                return Response(status_code=401)
            business = await session.scalar(
                select(Business).where(Business.slug == account.tenant_id)
            )
            if business is not None and business.status != "active":
                return Response(status_code=200)
        jobs = await prepare_webhook(services, channel, account.tenant_id, raw, str(account.id))
        if jobs:
            background_tasks.add_task(
                process_jobs, services, channel, account.tenant_id, dict(account.config), jobs
            )
        return Response(status_code=200)
