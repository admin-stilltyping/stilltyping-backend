"""Per-tenant business instructions.

Unlike knowledge (retrieved per query), instructions are always injected into
the system prompt, so hard rules apply on every turn regardless of the question.
The safety base prompt still takes precedence over anything stored here.
"""

from .db import TenantSettings


async def get_instructions(session, tenant: str) -> str:
    row = await session.get(TenantSettings, tenant)
    return row.instructions if row else ""


async def set_instructions(session, tenant: str, text: str) -> None:
    row = await session.get(TenantSettings, tenant)
    if row is None:
        session.add(TenantSettings(tenant_id=tenant, instructions=text))
    else:
        row.instructions = text
    await session.flush()
