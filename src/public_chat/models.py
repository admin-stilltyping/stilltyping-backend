from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class VisitorSession(TimestampMixin, Base):
    __tablename__ = "web_chat_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_web_chat_sessions_business", "business_id"),)


class VisitorTurn(TimestampMixin, Base):
    __tablename__ = "web_chat_turns"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("web_chat_sessions.id", ondelete="CASCADE"))
    request_id: Mapped[UUID] = mapped_column()
    seq: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    __table_args__ = (
        UniqueConstraint("session_id", "request_id"),
        UniqueConstraint("session_id", "seq"),
        CheckConstraint("status IN ('pending','complete','failed')"),
        Index("ix_web_chat_turns_session_created", "session_id", "created_at"),
    )
