from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from context_agent.db import ChannelAccount
from context_agent.schemas import DomainError
from custom_fields.service import Owner

router = APIRouter(prefix="/admin/{slug}/channels", tags=["Business integrations"])
REQUIRED = {
    "instagram": ("access_token", "app_secret", "verify_token"),
    "whatsapp": ("access_token", "app_secret", "verify_token"),
    "telegram": ("bot_token", "webhook_secret"),
    "razorpay": ("key_id", "key_secret", "webhook_secret"),
}


class ChannelOutput(BaseModel):
    channel_type: Literal["instagram", "whatsapp", "telegram", "razorpay"]
    external_channel_id: str
    is_active: bool
    configured: bool
    webhook_url_path: str


class InstagramInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    account_id: str = Field(pattern=r"^[0-9]{1,50}$")
    access_token: Annotated[SecretStr, Field(max_length=8192)] | None = None
    app_secret: Annotated[SecretStr, Field(max_length=512)] | None = None
    verify_token: Annotated[SecretStr, Field(max_length=256)] | None = None

    @field_validator("access_token", "app_secret", "verify_token", mode="before")
    @classmethod
    def clean_secret(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Credentials cannot contain control characters.")
            return value or None
        return value


def channel_view(row):
    # Telegram's routing ID IS its webhook secret; never expose that as an ID.
    return ChannelOutput(
        channel_type=row.channel,
        external_channel_id=row.account_id if row.channel in {"instagram", "whatsapp"} else "",
        is_active=True,
        configured=all(row.config.get(key) for key in REQUIRED[row.channel]),
        webhook_url_path=f"/webhooks/{row.channel}",
    )


async def instagram_account(session, tenant):
    rows = list(
        await session.scalars(
            select(ChannelAccount)
            .where(
                ChannelAccount.tenant_id == tenant,
                ChannelAccount.channel == "instagram",
            )
            .limit(2)
        )
    )
    if len(rows) > 1:
        raise DomainError(
            409,
            "multiple_instagram_accounts",
            "Multiple Instagram accounts are registered. Ask your platform admin to select one before editing.",
        )
    return rows[0] if rows else None


@router.get("", response_model=list[ChannelOutput])
async def list_channels(identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction() as session:
        rows = await session.scalars(
            select(ChannelAccount)
            .where(
                ChannelAccount.tenant_id == identity.business.slug,
                ChannelAccount.channel.in_(REQUIRED),
            )
            .order_by(ChannelAccount.channel, ChannelAccount.created_at)
        )
        return [channel_view(row) for row in rows]


@router.put("/instagram", response_model=ChannelOutput)
async def save_instagram(payload: InstagramInput, identity: Owner, request: Request):
    tenant = identity.business.slug
    try:
        async with request.app.state.services["db"].transaction(tenant) as session:
            row = await instagram_account(session, tenant)
            if row and row.account_id != payload.account_id:
                raise DomainError(
                    409,
                    "instagram_account_changed",
                    "Disconnect the current Instagram account before adding a different account.",
                )
            # An owner must never reassign a different business's Instagram account.
            conflict = await session.scalar(
                select(ChannelAccount.id).where(
                    ChannelAccount.channel == "instagram",
                    ChannelAccount.account_id == payload.account_id,
                    ChannelAccount.tenant_id != tenant,
                )
            )
            if conflict:
                raise DomainError(
                    409, "instagram_account_in_use", "This Instagram account is already configured."
                )
            config = dict(row.config) if row else {}
            for key in REQUIRED["instagram"]:
                value = getattr(payload, key)
                if value is not None:
                    config[key] = value.get_secret_value()
            if not all(config.get(key) for key in REQUIRED["instagram"]):
                raise DomainError(
                    400,
                    "instagram_credentials_required",
                    "Account ID, access token, app secret and verify token are required for initial setup.",
                )
            config["account_id"] = payload.account_id
            if row is None:
                row = ChannelAccount(
                    tenant_id=tenant,
                    channel="instagram",
                    account_id=payload.account_id,
                    config=config,
                )
                session.add(row)
            else:
                row.config = config
            await session.flush()
            return channel_view(row)
    except IntegrityError:
        raise DomainError(
            409, "instagram_account_in_use", "This Instagram account is already configured."
        ) from None


@router.delete("/instagram", status_code=204)
async def disconnect_instagram(identity: Owner, request: Request):
    async with request.app.state.services["db"].transaction(identity.business.slug) as session:
        row = await instagram_account(session, identity.business.slug)
        if row is not None:
            await session.delete(row)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
