"""Business-owner notification history and a durable per-device push outbox."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now():
    return datetime.now(UTC)


class Notification(Base):
    __tablename__ = "business_notifications"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_notifications_business_created", "business_id", "created_at"),)


class PushSubscription(Base):
    __tablename__ = "admin_push_subscriptions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    admin_id: Mapped[UUID] = mapped_column(ForeignKey("business_admins.id", ondelete="CASCADE"))
    token_version: Mapped[int] = mapped_column(Integer)
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(100))
    auth: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_push_subscriptions_owner", "business_id", "admin_id"),)


class PushDelivery(Base):
    __tablename__ = "admin_push_deliveries"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_notifications.id", ondelete="CASCADE")
    )
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_push_subscriptions.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(30))
    __table_args__ = (
        UniqueConstraint("notification_id", "subscription_id"),
        Index("ix_push_delivery_pending", "status", "next_attempt_at"),
    )
