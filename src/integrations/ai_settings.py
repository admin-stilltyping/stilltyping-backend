"""Super-admin-only configuration. Stage embeddings before atomic activation."""

import asyncio
import copy
import json
import logging
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import select

from context_agent.db import KnowledgeUnit, Tool, embedding_text
from context_agent.models import Models
from context_agent.schemas import DomainError, StrictModel
from super_admin.businesses.service import get_business
from super_admin.routes import no_cache, require_super_admin

from .gemini import cipher, decrypt_key, encrypt_key
from .models import BusinessAISettings, BusinessGeminiCredential

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/super-admin/businesses",
    tags=["Business AI settings"],
    dependencies=[Depends(require_super_admin)],
)


class AIInput(StrictModel):
    revision: str = Field(max_length=36)
    llm_provider: Literal["gemini", "deepseek", "deepinfra"]
    llm_model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._/-]+$")
    llm_api_key: SecretStr | None = Field(default=None, max_length=512)
    embedding_provider: Literal["gemini", "deepinfra"]
    embedding_model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._/-]+$")
    embedding_dimensions: int = Field(ge=32, le=4096)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=512)
    relevance_threshold: float = Field(default=0.60, ge=-1, le=1)

    @field_validator("llm_api_key", "embedding_api_key", mode="before")
    @classmethod
    def secret(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("API keys cannot contain whitespace or control characters")
            return value or None
        return value


def default_config(settings):
    return dict(
        llm_provider="gemini",
        llm_model=settings.chat_model,
        embedding_provider="gemini",
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        relevance_threshold=settings.relevance_threshold,
    )


def view(settings, row):
    try:
        cipher(settings)
        editable = True
    except DomainError:
        editable = False
    return {
        "revision": row.revision if row else "",
        "source": "business" if row else "inherited",
        "can_update": editable,
        **(row.configuration if row else default_config(settings)),
    }


def model_settings(settings, config, keys, prefix):
    return settings.model_copy(
        update={
            "llm_provider": config["llm_provider"],
            "chat_model": config["llm_model"],
            "llm_api_key": SecretStr(keys["llm"]),
            "embedding_provider": config["embedding_provider"],
            "embedding_model": config["embedding_model"],
            "embedding_dimensions": config["embedding_dimensions"],
            "embedding_api_key": SecretStr(keys["embedding"]),
            "relevance_threshold": config["relevance_threshold"],
            "index_prefix": prefix,
        }
    )


def scoped_vectors(vectors, prefix, dimensions):
    result = copy.copy(vectors)
    result.prefix, result.dimensions = prefix, dimensions
    return result


async def snapshot(session, tenant):
    knowledge = list(
        await session.scalars(select(KnowledgeUnit).where(KnowledgeUnit.tenant_id == tenant))
    )
    tools = list(await session.scalars(select(Tool).where(Tool.tenant_id.in_([tenant, "general"]))))
    stamp = sorted(("knowledge", str(x.id), x.title, x.content) for x in knowledge)
    stamp += sorted(("tool", str(x.id), x.name, x.description) for x in tools)
    return knowledge, tools, stamp


async def validate_llm(config, key):
    provider = config["llm_provider"]
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + config["llm_model"].removeprefix("models/")
        if provider == "gemini"
        else (
            "https://api.deepinfra.com/v1/openai/models"
            if provider == "deepinfra"
            else "https://api.deepseek.com/models"
        )
    )
    headers = (
        {"x-goog-api-key": key} if provider == "gemini" else {"Authorization": f"Bearer {key}"}
    )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise DomainError(
                400,
                "ai_validation_failed",
                "The LLM provider rejected the key or model. Check access and quota.",
            )
        if provider != "gemini" and config["llm_model"] not in {
            x["id"] for x in response.json().get("data", [])
        }:
            raise DomainError(
                400, "ai_model_unavailable", "This model is not available to the LLM key."
            )
    except httpx.HTTPError:
        raise DomainError(
            503, "ai_validation_unavailable", "Could not reach the LLM provider."
        ) from None


@router.get("/{slug}/ai-settings")
async def get_ai(slug: str, request: Request, response: Response):
    no_cache(response)
    services = request.app.state.services
    async with services["db"].transaction() as session:
        business = await get_business(session, slug)
        row = await session.get(BusinessAISettings, business.id)
        return view(services["settings"], row)


@router.put("/{slug}/ai-settings")
async def save_ai(slug: str, payload: AIInput, request: Request, response: Response):
    no_cache(response)
    services = request.app.state.services
    db, settings = services["db"], services["settings"]
    cipher(settings)
    config = payload.model_dump(exclude={"revision", "llm_api_key", "embedding_api_key"})
    if config["embedding_provider"] == "deepinfra":
        sizes = {
            "Qwen/Qwen3-Embedding-0.6B": 1024,
            "Qwen/Qwen3-Embedding-4B": 2560,
            "Qwen/Qwen3-Embedding-8B": 4096,
        }
        if (
            config["embedding_model"] not in sizes
            or config["embedding_dimensions"] > sizes[config["embedding_model"]]
        ):
            raise DomainError(
                400,
                "invalid_embedding_model",
                "Select a supported Qwen embedding model and dimension.",
            )
    async with db.transaction(slug) as session:
        business = await get_business(session, slug)
        old = await session.get(BusinessAISettings, business.id)
        if (old.revision if old else "") != payload.revision:
            raise DomainError(409, "ai_settings_changed", "Settings changed. Reload and try again.")
        old_config = old.configuration if old else default_config(settings)
        legacy = await session.get(BusinessGeminiCredential, business.id)
        inherited = (
            decrypt_key(settings, legacy)
            if legacy
            else (settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None)
        )
        old_keys = (
            json.loads(decrypt_key(settings, old))
            if old
            else {"llm": inherited, "embedding": inherited}
        )
        keys = {}
        for kind in ("llm", "embedding"):
            supplied = getattr(payload, f"{kind}_api_key")
            keys[kind] = (
                supplied.get_secret_value()
                if supplied
                else (
                    old_keys[kind]
                    if config[f"{kind}_provider"] == old_config[f"{kind}_provider"]
                    else None
                )
            )
            if not keys[kind]:
                raise DomainError(400, "ai_key_required", f"Provide an API key for {kind}.")
            config[f"{kind}_key_last_four"] = keys[kind][-4:]
        rebuild = any(
            config[k] != old_config[k]
            for k in ("embedding_provider", "embedding_model", "embedding_dimensions")
        )
        prefix = (
            f"business_{business.id.hex}_{uuid4().hex}"
            if rebuild
            else (old.index_prefix if old else settings.index_prefix)
        )
        knowledge, tools, before = await snapshot(session, slug)
    runtime_settings = model_settings(settings, config, keys, prefix)
    models = Models(runtime_settings)
    vectors = scoped_vectors(services["vectors"], prefix, config["embedding_dimensions"])
    try:
        async with asyncio.timeout(150):
            await validate_llm(config, keys["llm"])
            # This small request validates actual embedding access/dimensions; it is billable.
            await models.query("connection test")
            if rebuild:
                await vectors.initialize()
                for kind, rows in (("knowledge_units", knowledge), ("tools", tools)):
                    for start in range(0, len(rows), 16):
                        batch = rows[start : start + 16]
                        texts = [
                            embedding_text(x.title, x.content)
                            if kind == "knowledge_units"
                            else embedding_text(x.name, x.description)
                            for x in batch
                        ]
                        await vectors.upsert(kind, batch, await models.embed(texts))
        async with db.transaction(slug) as session:
            current = await session.get(BusinessAISettings, business.id)
            _, _, after = await snapshot(session, slug)
            if (current.revision if current else "") != payload.revision or after != before:
                raise DomainError(
                    409,
                    "ai_settings_changed",
                    "Knowledge or settings changed during preparation. Try again.",
                )
            if current is None:
                current = BusinessAISettings(business_id=business.id)
                session.add(current)
            current.configuration = config
            current.encrypted_key = encrypt_key(settings, business.id, json.dumps(keys))
            current.index_prefix = prefix
            current.revision = str(uuid4())
            await session.flush()
            return view(settings, current)
    except TimeoutError:
        raise DomainError(
            503, "ai_setup_timeout", "Preparation timed out. Existing settings are unchanged."
        ) from None
    finally:
        try:
            await models.close()
        except Exception as exc:
            log.warning("AI client cleanup failed: %s", type(exc).__name__)
