"""Business-owned Gemini credentials. Never return or log plaintext secrets."""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from context_agent.schemas import DomainError
from custom_fields.service import Owner

from .models import BusinessGeminiCredential

router = APIRouter(prefix="/admin/{slug}/integrations/gemini", tags=["Business integrations"])


def cipher(settings):
    secret = settings.integration_encryption_key
    try:
        if secret:
            return Fernet(secret.get_secret_value().encode())
    except (ValueError, TypeError):
        pass
    raise DomainError(
        503,
        "integration_storage_unavailable",
        "API key storage is not configured. Please contact your platform administrator.",
    )


def encrypt_key(settings, business_id, key):
    # Bind the encrypted payload to its owner as well as encrypting the secret.
    payload = json.dumps({"business_id": str(business_id), "key": key}).encode()
    return cipher(settings).encrypt(payload).decode()


def decrypt_key(settings, row):
    try:
        payload = json.loads(cipher(settings).decrypt(row.encrypted_key.encode()))
        if payload["business_id"] != str(row.business_id):
            raise ValueError("Credential owner mismatch")
        return payload["key"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise DomainError(
            503,
            "integration_key_unavailable",
            "The saved Gemini key could not be loaded. Update it in Integrations.",
        ) from None


class GeminiInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: Annotated[SecretStr, Field(min_length=20, max_length=256)]

    @field_validator("api_key", mode="before")
    @classmethod
    def clean_key(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", value.strip()):
            raise ValueError("Enter a valid Gemini API key.")
        return value.strip()


class GeminiOutput(BaseModel):
    source: Literal["business", "platform", "unconfigured"]
    key_last_four: str | None
    updated_at: datetime | None
    can_update: bool


def credential_view(settings, row):
    try:
        cipher(settings)
        can_update = True
    except DomainError:
        can_update = False
    platform = settings.gemini_api_key and settings.gemini_api_key.get_secret_value()
    return GeminiOutput(
        source="business" if row else "platform" if platform else "unconfigured",
        key_last_four=row.key_last_four if row else None,
        updated_at=row.updated_at if row else None,
        can_update=can_update,
    )


async def validate_key(key, settings):
    # Metadata checks use a fixed Google destination and a header, never a key in
    # a URL. Do not send customer content or generate a billable test response.
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for model in dict.fromkeys([settings.chat_model, settings.embedding_model]):
            name = quote(model.removeprefix("models/"), safe="")
            try:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{name}",
                    headers={"x-goog-api-key": key},
                )
            except httpx.HTTPError:
                raise DomainError(
                    503,
                    "gemini_verification_unavailable",
                    "Could not reach Gemini to verify this key. Try again shortly.",
                ) from None
            if response.status_code in {400, 401, 403}:
                raise DomainError(
                    400,
                    "invalid_gemini_key",
                    "Gemini rejected this key. Check that it has Gemini API access.",
                )
            if response.status_code == 404:
                raise DomainError(
                    400,
                    "gemini_model_unavailable",
                    "This key cannot access the platform's default models. Contact your platform administrator.",
                )
            if response.status_code != 200:
                raise DomainError(
                    503,
                    "gemini_verification_unavailable",
                    "Gemini could not verify this key right now. Try again shortly.",
                )


@router.get("", response_model=GeminiOutput)
async def get_gemini(identity: Owner, request: Request):
    services = request.app.state.services
    async with services["db"].transaction() as session:
        row = await session.get(BusinessGeminiCredential, identity.business.id)
        return credential_view(services["settings"], row)


@router.put("", response_model=GeminiOutput)
async def save_gemini(payload: GeminiInput, identity: Owner, request: Request):
    services = request.app.state.services
    settings, business = services["settings"], identity.business
    key = payload.api_key.get_secret_value()
    encrypted = encrypt_key(settings, business.id, key)
    await validate_key(key, settings)
    # The old key stays intact if validation fails. Runtime resolves it on the
    # next AI operation, so updates work across all Vercel instances immediately.
    async with services["db"].transaction(business.slug) as session:
        row = await session.get(BusinessGeminiCredential, business.id)
        if row is None:
            row = BusinessGeminiCredential(business_id=business.id)
            session.add(row)
        row.encrypted_key = encrypted
        row.key_last_four = key[-4:]
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return credential_view(settings, row)
