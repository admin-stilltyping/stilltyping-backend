"""Inbound social-channel webhooks (WhatsApp, Instagram, Telegram).

Flow per delivery:
  1. Resolve the ChannelAccount from the platform's routing key (unverified read).
  2. Verify the signature / secret against that account's stored credentials.
  3. Return 200 immediately; process in the background so the platform never
     times out and redelivers.
  4. Dedup on the platform message id (webhook_events), then run the same agent
     conversation loop as the API, and send the reply back on the same channel.
"""

import logging
from uuid import NAMESPACE_URL, uuid5

from fastapi import BackgroundTasks, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import select

from .channels import ADAPTERS
from .db import ChannelAccount, WebhookEvent
from .schemas import ChatInput

log = logging.getLogger(__name__)


async def _accounts_for_channel(session, channel):
    return list(
        (await session.scalars(select(ChannelAccount).where(ChannelAccount.channel == channel))).all()
    )


async def _lookup_account(session, channel, account_id):
    if not account_id:
        return None
    return await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.channel == channel, ChannelAccount.account_id == account_id
        )
    )


async def _seen(session, channel, event_id) -> bool:
    return (
        await session.scalar(
            select(WebhookEvent.id).where(
                WebhookEvent.channel == channel, WebhookEvent.event_id == event_id
            )
        )
    ) is not None


async def process_webhook(services, channel: str, tenant: str, config: dict, raw: bytes) -> None:
    adapter = ADAPTERS[channel]
    db, agent = services["db"], services["agent"]
    for message in adapter.parse(raw):
        if not message.text or not message.external_user_id:
            continue
        if message.message_id:
            # Dedup + claim under the tenant lock so a redelivery cannot double-answer.
            async with db.transaction(tenant) as session:
                if await _seen(session, channel, message.message_id):
                    log.info("webhook_duplicate channel=%s id=%s", channel, message.message_id)
                    continue
                session.add(WebhookEvent(channel=channel, event_id=message.message_id))
        try:
            request_id = uuid5(
                NAMESPACE_URL, f"context-agent:webhook:{channel}:{message.message_id}"
            )
            payload = ChatInput(
                message=message.text,
                request_id=request_id,
                channel=channel,
                external_user_id=message.external_user_id,
            )
        except ValidationError:
            log.warning("webhook_invalid_message channel=%s", channel)
            continue
        try:
            result = await agent.run(tenant, payload)
        except Exception:
            log.exception("webhook_agent_failed channel=%s tenant=%s", channel, tenant)
            continue
        try:
            await adapter.send(config, message.external_user_id, result["answer"])
        except Exception:
            log.exception("webhook_send_failed channel=%s tenant=%s", channel, tenant)


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
        key = adapter.routing_key(request.headers, raw)
        async with services["db"].transaction() as session:
            account = await _lookup_account(session, channel, key)
        if account is None:
            # Unknown account: ack so the platform stops retrying, but do nothing.
            log.warning("webhook_unknown_account channel=%s", channel)
            return Response(status_code=200)
        if not adapter.verify(request.headers, raw, account.config):
            log.warning("webhook_signature_invalid channel=%s", channel)
            return Response(status_code=401)
        # Snapshot tenant + config: the account row's session closes before the task runs.
        background_tasks.add_task(
            process_webhook, services, channel, account.tenant_id, dict(account.config), raw
        )
        return Response(status_code=200)
