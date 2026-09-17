import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from jsonschema import Draft202012Validator
from sqlalchemy import select

from modules.service import support_enabled

from . import support
from .db import Tool, embedding_text
from .schemas import DomainError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    request_id: UUID
    call_id: str
    settings: object
    db: object = None
    channel: str = "web"
    external_user_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters_schema: dict
    handler_key: str
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    tenant_id: str = "general"

    @property
    def id(self):
        return uuid5(NAMESPACE_URL, f"context-agent:tool:{self.tenant_id}:{self.name}")


async def _notify_webhook(settings, context, args, ticket_ref):
    # Best-effort external notification; the internal ticket is the source of truth.
    headers = {"Idempotency-Key": str(uuid5(context.request_id, context.tenant_id + ":support"))}
    if settings.support_webhook_token:
        headers["Authorization"] = "Bearer " + settings.support_webhook_token.get_secret_value()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(
            settings.support_webhook_url,
            json={
                "tenant_id": context.tenant_id,
                "request_id": str(context.request_id),
                "ticket_ref": ticket_ref,
                **args,
            },
            headers=headers,
        )
        response.raise_for_status()


async def support_ticket(args, context):
    if context.db is None:
        return {"ok": False, "error": "Support storage is unavailable."}
    # Durable internal ticket, idempotent on (tenant, request_id), under the tenant lock.
    async with context.db.transaction(context.tenant_id) as session:
        if not await support_enabled(session, context.tenant_id):
            return {"ok": False, "error": "Support tickets are disabled for this business."}
        ticket = await support.create_or_get(
            session,
            context.tenant_id,
            context.request_id,
            question=args["question"],
            reason=args["reason"],
            channel=context.channel,
            external_user_id=context.external_user_id,
        )
        ticket_ref = ticket.ticket_ref
    if context.settings.support_webhook_url:
        try:
            await _notify_webhook(context.settings, context, args, ticket_ref)
        except Exception:
            log.warning("Support webhook notification failed for ticket %s", ticket_ref)
    return {"ok": True, "ticket_id": ticket_ref}


SUPPORT = ToolDefinition(
    name="create_support_ticket",
    description="Create a support ticket when supplied knowledge and available tool results cannot "
    "support an answer to the user's request.",
    parameters_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["question", "reason"],
        "additionalProperties": False,
    },
    handler_key="support.create_ticket",
    handler=support_ticket,
)

# Add developer-authored ToolDefinition objects here. No dynamic imports from database values.
REGISTRY = [SUPPORT]


def resolve_definition(row, registry=REGISTRY):
    for definition in registry:
        if (
            definition.id == row.id
            and definition.handler_key == row.handler_key
            and definition.name == row.name
            and definition.tenant_id == row.tenant_id
            and definition.description == row.description
            and definition.parameters_schema == row.parameters_schema
        ):
            return definition
    return None


async def execute(definition, args, context):
    if definition.tenant_id not in ("general", context.tenant_id):
        raise DomainError(400, "invalid_tool", "Tool is outside this tenant's scope.")
    Draft202012Validator(definition.parameters_schema).validate(args)
    try:
        result = await asyncio.wait_for(definition.handler(args, context), timeout=25)
        if not isinstance(result, dict):
            raise ValueError("Tool results must be JSON objects")
        if len(json.dumps(result)) > 30000:
            return {"ok": False, "error": "Tool response exceeded the allowed size."}
        return result
    except Exception:
        # Never claim a timed-out or failed external action succeeded; no automatic mutation retry.
        return {"ok": False, "error": "Tool execution failed or its outcome is unknown."}


async def sync_tools(db, models, vectors, registry=REGISTRY, *, force_reembed=False):
    identities = [(x.tenant_id, x.name) for x in registry]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate tool identity in code registry")
    for definition in registry:
        Draft202012Validator.check_schema(definition.parameters_schema)
        async with db.transaction("tool-sync:" + definition.tenant_id) as session:
            row = await session.get(Tool, definition.id)
            changed = (
                row is None
                or row.name != definition.name
                or row.description != definition.description
            )
            needs_embedding = force_reembed or changed or row.embedding_status != "ready"
            if row is None:
                row = Tool(id=definition.id, tenant_id=definition.tenant_id)
                session.add(row)
            row.name, row.description = definition.name, definition.description
            row.parameters_schema, row.handler_key = (
                definition.parameters_schema,
                definition.handler_key,
            )
            if needs_embedding:
                row.embedding_status = "pending"
        if needs_embedding:
            async with db.transaction("tool-sync:" + definition.tenant_id) as session:
                row = await session.get(Tool, definition.id)
                if resolve_definition(row, registry) is None:
                    raise DomainError(409, "concurrent_update", "Tool changed during sync.")
                embeddings = await models.embed([embedding_text(row.name, row.description)])
                await vectors.upsert("tools", [row], embeddings)
                row.embedding_status = "ready"
    # Definitions removed from code cannot execute, even while their old index points remain.
    return {"synced": len(registry)}


async def available_tools(session, tenant, retrieved, registry=REGISTRY):
    # Resolve tenant overrides before relevance selection, so a global duplicate cannot leak through.
    rows = list(
        (
            await session.scalars(
                select(Tool).where(
                    Tool.tenant_id.in_(["general", tenant]), Tool.embedding_status == "ready"
                )
            )
        ).all()
    )
    eligible = {row.id: row for row in rows if resolve_definition(row, registry)}
    overrides = {row.name: row for row in eligible.values() if row.tenant_id == tenant}
    selected = {}
    retrieved_ids = {row.id for row in retrieved}
    for row in retrieved:
        if row.id not in eligible:
            continue
        effective = overrides.get(row.name, row)
        if effective.id not in retrieved_ids:
            continue  # A tenant override must pass retrieval filtering itself.
        selected[effective.name] = resolve_definition(effective, registry)
    # The code-defined fallback respects module access, independently of retrieval/tool sync.
    if await support_enabled(session, tenant):
        selected[SUPPORT.name] = SUPPORT
    else:
        selected.pop(SUPPORT.name, None)
    return selected
