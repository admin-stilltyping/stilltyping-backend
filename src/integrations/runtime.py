"""Resolve a business key once per AI operation, with isolated SDK clients."""

import logging
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy import select

from context_agent.agent import Agent
from context_agent.knowledge import KnowledgeService
from context_agent.models import Models
from context_agent.retrieval import Retriever
from context_agent.schemas import DomainError
from super_admin.businesses.models import Business

from .gemini import decrypt_key
from .models import BusinessGeminiCredential

log = logging.getLogger(__name__)


class BusinessAI:
    def __init__(self, db, vectors, settings, default_models):
        self.db, self.vectors, self.settings = db, vectors, settings
        self.default_models = default_models
        self.default_agent = None
        self.default_knowledge = None
        if default_models is not None:
            retriever = self.retriever(default_models)
            self.default_agent = Agent(db, default_models, retriever, settings)
            self.default_knowledge = KnowledgeService(db, default_models, vectors, retriever)

    @asynccontextmanager
    async def models(self, tenant):
        async with self.db.transaction() as session:
            row = await session.scalar(
                select(BusinessGeminiCredential)
                .join(Business, Business.id == BusinessGeminiCredential.business_id)
                .where(Business.slug == tenant)
            )
        if row is None:
            if self.default_models is None:
                raise DomainError(
                    503,
                    "gemini_not_configured",
                    "Add a Gemini API key in Integrations to enable AI replies.",
                )
            yield self.default_models
            return
        key = decrypt_key(self.settings, row)
        models = Models(self.settings.model_copy(update={"gemini_api_key": SecretStr(key)}))
        try:
            yield models
        finally:
            try:
                await models.close()
            except Exception as exc:
                # Cleanup must not turn a saved AI reply into a retryable failure.
                log.warning("Gemini client cleanup failed: %s", type(exc).__name__)

    def retriever(self, models):
        return Retriever(models, self.vectors, self.settings)

    async def run(self, tenant, request, **kwargs):
        # Admin Agent Chat, public chat and messaging webhooks all use this entry.
        async with self.models(tenant) as models:
            agent = (
                self.default_agent
                if models is self.default_models
                else Agent(self.db, models, self.retriever(models), self.settings)
            )
            return await agent.run(tenant, request, **kwargs)

    async def _knowledge(self, method, tenant, payload, **kwargs):
        async with self.models(tenant) as models:
            knowledge = (
                self.default_knowledge
                if models is self.default_models
                else KnowledgeService(self.db, models, self.vectors, self.retriever(models))
            )
            return await getattr(knowledge, method)(tenant, payload, **kwargs)

    async def put(self, tenant, payload, **kwargs):
        return await self._knowledge("put", tenant, payload, **kwargs)

    async def add(self, tenant, payload):
        return await self._knowledge("add", tenant, payload)

    async def update(self, tenant, payload):
        return await self._knowledge("update", tenant, payload)
