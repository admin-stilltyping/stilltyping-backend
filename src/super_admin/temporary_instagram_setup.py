"""Time-bounded, authenticated setup for the explicitly requested Instagram account."""

import hashlib
import hmac
import os
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from context_agent.db import ChannelAccount
from context_agent.schemas import DomainError
from super_admin.businesses.models import Business
from super_admin.routes import no_cache, require_super_admin

BUSINESS_ID = UUID("4c5d16c3-0876-4dc2-956f-f06684445cb9")
BUSINESS_SLUG = "dental"
ACCOUNT_ID = "17841427926855776"
TOKEN_SHA256 = "8108af0b5bc3ed83c21b73b19f3cd76b8baa416674625dced60bbd91d398c31b"
EXPIRES_AT = datetime.fromisoformat("2026-09-18T12:10:00+00:00")

router = APIRouter(
    prefix="/super-admin/setup",
    dependencies=[Depends(require_super_admin)],
)


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verify_token: SecretStr = Field(min_length=32, max_length=256)


@router.put("/instagram-verification")
async def configure_verification(payload: VerificationInput, request: Request, response: Response):
    no_cache(response)
    if os.environ.get("VERCEL_ENV") != "production" or datetime.now(UTC) >= EXPIRES_AT:
        raise DomainError(410, "setup_expired", "This one-time setup is unavailable.")
    token = payload.verify_token.get_secret_value()
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), TOKEN_SHA256):
        raise DomainError(400, "unexpected_token", "The token does not match the requested setup.")
    db = request.app.state.services["db"]
    async with db.transaction(BUSINESS_SLUG) as session:
        business = await session.get(Business, BUSINESS_ID)
        if not business or business.slug != BUSINESS_SLUG or business.status != "active":
            raise DomainError(
                409, "unexpected_business", "The intended active business was not found."
            )
        account = await session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.channel == "instagram", ChannelAccount.account_id == ACCOUNT_ID
            )
        )
        if account and account.tenant_id != BUSINESS_SLUG:
            raise DomainError(409, "account_in_use", "This account belongs to another business.")
        other = await session.scalar(
            select(ChannelAccount.id)
            .where(
                ChannelAccount.channel == "instagram",
                ChannelAccount.tenant_id == BUSINESS_SLUG,
                ChannelAccount.account_id != ACCOUNT_ID,
            )
            .limit(1)
        )
        if other:
            raise DomainError(
                409, "existing_account", "This business already has another Instagram account."
            )
        created = account is None
        if account is None:
            account = ChannelAccount(
                tenant_id=BUSINESS_SLUG, channel="instagram", account_id=ACCOUNT_ID, config={}
            )
            session.add(account)
        config = dict(account.config)
        if config.get("account_id") not in (None, ACCOUNT_ID):
            raise DomainError(409, "account_mismatch", "The saved account routing is inconsistent.")
        account.config = {**config, "account_id": ACCOUNT_ID, "verify_token": token}
        await session.flush()
        result = {
            "business": BUSINESS_SLUG,
            "instagram_account_id": ACCOUNT_ID,
            "verification_saved": True,
            "created": created,
            "messaging_credentials_present": bool(
                config.get("app_secret") and config.get("access_token")
            ),
        }
    return result
