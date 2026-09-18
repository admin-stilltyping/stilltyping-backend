"""Authenticated, time-bounded update of the requested Instagram app secret."""

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
SECRET_SHA256 = "333aefb19396531647b8801295f95b21b35307bc304cb2b7fc4e0f1c4b76486a"
EXPIRES_AT = datetime.fromisoformat("2026-09-18T12:15:00+00:00")
router = APIRouter(prefix="/super-admin/setup", dependencies=[Depends(require_super_admin)])


class AppSecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_secret: SecretStr = Field(min_length=32, max_length=32)


@router.put("/instagram-app-secret")
async def save_app_secret(payload: AppSecretInput, request: Request, response: Response):
    no_cache(response)
    if os.environ.get("VERCEL_ENV") != "production" or datetime.now(UTC) >= EXPIRES_AT:
        raise DomainError(410, "setup_expired", "This one-time setup is unavailable.")
    secret = payload.app_secret.get_secret_value()
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), SECRET_SHA256):
        raise DomainError(
            400, "unexpected_secret", "The secret does not match the requested setup."
        )
    db = request.app.state.services["db"]
    async with db.transaction(BUSINESS_SLUG) as session:
        business = await session.get(Business, BUSINESS_ID)
        if not business or business.slug != BUSINESS_SLUG or business.status != "active":
            raise DomainError(
                409, "unexpected_business", "The intended active business was not found."
            )
        account = await session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.channel == "instagram",
                ChannelAccount.account_id == ACCOUNT_ID,
            )
        )
        if account is None or account.tenant_id != BUSINESS_SLUG:
            raise DomainError(
                409, "unexpected_account", "The intended Instagram account was not found."
            )
        config = dict(account.config)
        if config.get("account_id") != ACCOUNT_ID or not config.get("verify_token"):
            raise DomainError(
                409, "incomplete_account", "The prior account verification setup was not found."
            )
        account.config = {**config, "app_secret": secret}
        await session.flush()
        result = {
            "business": BUSINESS_SLUG,
            "instagram_account_id": ACCOUNT_ID,
            "app_secret_saved": True,
            "access_token_configured": bool(config.get("access_token")),
            "verify_token_preserved": True,
        }
    return result
