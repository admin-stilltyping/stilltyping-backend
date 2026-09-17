from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class Business(TimestampMixin, Base):
    __tablename__ = "businesses"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Kolkata")
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    plan: Mapped[str | None] = mapped_column(String(20), default="free")


class BusinessAdmin(TimestampMixin, Base):
    __tablename__ = "business_admins"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), unique=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    token_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
