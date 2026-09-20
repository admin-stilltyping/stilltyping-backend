import hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from .performance import current_timing, instrument_engine, measure, measure_exit


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(Text)
    source_text: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)


class KnowledgeUnit(TimestampMixin, Base):
    __tablename__ = "knowledge_units"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200))
    document_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_status: Mapped[str] = mapped_column(String(10), default="pending")
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("embedding_status IN ('pending','ready','failed')"),
        Index("ix_units_tenant_document", "tenant_id", "document_id"),
    )


class Tool(TimestampMixin, Base):
    __tablename__ = "tools"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    parameters_schema: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    handler_key: Mapped[str] = mapped_column(String(200))
    embedding_status: Mapped[str] = mapped_column(String(10), default="pending")
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint("embedding_status IN ('pending','ready','failed')"),
    )


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "external_id"),
        Index("ix_conversations_tenant", "tenant_id"),
    )


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200))
    conversation_id: Mapped[UUID] = mapped_column(Uuid)
    # Per-conversation monotonic order, MAX(seq)+1 under the tenant advisory lock.
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        CheckConstraint("role IN ('user','assistant')"),
        UniqueConstraint("conversation_id", "seq"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


class GeminiContextCache(Base):
    """One current provider cache per tenant; no prompt text or credentials are stored."""

    __tablename__ = "gemini_context_caches"
    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    cache_name: Mapped[str | None] = mapped_column(String(300))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_count: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiUsageRecord(Base):
    """One actual agent invocation, including all model/tool rounds."""

    __tablename__ = "ai_usage_records"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid)
    channel: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    tokens_complete: Mapped[bool] = mapped_column(Boolean)
    llm_calls: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    # Phase breakdown of duration_ms, for locating where a slow reply spent its time.
    # Null on rows written before this instrumentation existed. queue_ms and send_ms
    # only apply to channel deliveries (webhooks); portal/API calls leave them null.
    queue_ms: Mapped[float | None] = mapped_column(Float)
    db_ms: Mapped[float | None] = mapped_column(Float)
    ai_ms: Mapped[float | None] = mapped_column(Float)
    tool_ms: Mapped[float | None] = mapped_column(Float)
    send_ms: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        CheckConstraint("status IN ('completed','failed','awaiting_send','send_failed')"),
        CheckConstraint("duration_ms >= 0 AND input_tokens >= 0 AND output_tokens >= 0"),
        Index("ix_ai_usage_tenant_started", "tenant_id", "started_at", "id"),
        Index("ix_ai_usage_tenant_channel_started", "tenant_id", "channel", "started_at"),
    )


class ChannelAccount(TimestampMixin, Base):
    """Routes an inbound messaging account to a tenant and holds its credentials.

    account_id is the platform's own routing key: WhatsApp phone_number_id,
    Instagram account id, or the Telegram webhook secret token. config carries
    per-account secrets (access_token, app_secret, verify_token, bot_token, ...)
    and never comes from LLM input.
    """

    __tablename__ = "channel_accounts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20))
    account_id: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    __table_args__ = (
        UniqueConstraint("channel", "account_id"),
        Index("ix_channel_accounts_tenant", "tenant_id"),
    )


class WebhookEvent(Base):
    """Inbound message-level idempotency: platforms redeliver, so the same
    channel message id is processed at most once."""

    __tablename__ = "webhook_events"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    channel: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(200))
    # Old dedup-only rows have no owner and are deliberately excluded from the portal.
    tenant_id: Mapped[str | None] = mapped_column(String(200))
    external_event_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="legacy", server_default="legacy")
    deliveries: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(50))
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("channel", "event_id"),
        Index("ix_webhook_events_tenant_created", "tenant_id", "created_at", "id"),
    )


class SupportTicket(TimestampMixin, Base):
    """A durable escalation ticket, created when the agent cannot answer.

    Idempotent on (tenant_id, request_id): a retried request reuses the same
    ticket rather than opening a duplicate. The external SUPPORT webhook, when
    configured, is only a best-effort notification — this row is the record.
    """

    __tablename__ = "support_tickets"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200))
    ticket_ref: Mapped[str] = mapped_column(String(20))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    question: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="open")
    channel: Mapped[str | None] = mapped_column(String(20))
    external_user_id: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("ticket_ref"),
        CheckConstraint("status IN ('open','resolved')"),
        Index("ix_support_tickets_tenant_status", "tenant_id", "status"),
    )


class TenantSettings(TimestampMixin, Base):
    """Per-tenant agent configuration. `instructions` is the tenant's business
    prompt (persona, tone, hard rules) prepended to the safety base prompt on
    every turn — always present, unlike retrieved knowledge."""

    __tablename__ = "tenant_settings"
    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    instructions: Mapped[str] = mapped_column(Text, default="", server_default="")


def embedding_text(title: str, content: str) -> str:
    return f"{title.strip()}\n\n{content.strip()}"


def content_hash(title: str, content: str) -> str:
    return hashlib.sha256(embedding_text(title, content).encode()).hexdigest()


def database_engine(url):
    parsed = make_url(url)
    options = {"pool_pre_ping": True}
    if (
        parsed.drivername == "postgresql+asyncpg"
        and (parsed.host or "").endswith(".pooler.supabase.com")
        and parsed.port == 6543
    ):
        # Supavisor owns pooling. Serverless containers must not hold idle
        # sessions or reuse prepared statements across backend connections.
        options.update(
            poolclass=NullPool,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__agent_{uuid4().hex}__",
            },
        )
    return create_async_engine(url, **options)


class Database:
    def __init__(self, url: str):
        self.engine = database_engine(url)
        instrument_engine(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self, tenant: str | None = None):
        async with measure_exit(self.sessions()) as session, measure_exit(session.begin()):
            if current_timing.get() is not None:
                # Separate pool wait / pre-ping / new connection setup from SQL.
                # Ordinary requests retain SQLAlchemy's lazy connection acquisition.
                with measure("db_acquire"):
                    await session.connection()
            if tenant is not None and self.engine.dialect.name == "postgresql":
                # Serializes writes and consistent reads per tenant across service workers.
                lock = int.from_bytes(hashlib.sha256(tenant.encode()).digest()[:8], signed=True)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)").execution_options(
                        request_timing_kind="db_lock"
                    ),
                    {"key": lock},
                )
            yield session
        # Only a successfully committed notification wakes the delivery worker.
        if session.info.get("notify_push"):
            from .notifications import after_commit

            after_commit(self)

    async def close(self):
        await self.engine.dispose()
