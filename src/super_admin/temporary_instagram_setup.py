"""Authenticated, time-bounded update of the verified Instagram access token."""

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
TOKEN_SHA256 = "e9476bb50334cd8ac76e0231ce81c7998253b130791b98fd2f43516eda91ba28"
EXPIRES_AT = datetime.fromisoformat("2026-09-18T15:00:00+00:00")
router = APIRouter(prefix="/super-admin/setup", dependencies=[Depends(require_super_admin)])


class AccessTokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: SecretStr = Field(min_length=1, max_length=8192)


@router.put("/instagram-access-token")
async def save_access_token(payload: AccessTokenInput, request: Request, response: Response):
    no_cache(response)
    if os.environ.get("VERCEL_ENV") != "production" or datetime.now(UTC) >= EXPIRES_AT:
        raise DomainError(410, "setup_expired", "This one-time setup is unavailable.")
    token = payload.access_token.get_secret_value()
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
                ChannelAccount.channel == "instagram",
                ChannelAccount.account_id == ACCOUNT_ID,
            )
        )
        if account is None or account.tenant_id != BUSINESS_SLUG:
            raise DomainError(
                409, "unexpected_account", "The intended Instagram account was not found."
            )
        config = dict(account.config)
        if config.get("account_id") != ACCOUNT_ID or not all(
            config.get(k) for k in ("verify_token", "app_secret")
        ):
            raise DomainError(
                409, "incomplete_account", "The prior account verification setup was not found."
            )
        account.config = {**config, "access_token": token}
        await session.flush()
        result = {
            "business": BUSINESS_SLUG,
            "instagram_account_id": ACCOUNT_ID,
            "access_token_saved": True,
            "app_secret_preserved": True,
            "verify_token_preserved": True,
            "all_credentials_configured": True,
        }
    return result
