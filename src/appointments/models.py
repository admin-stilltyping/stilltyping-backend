from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    lead_id: Mapped[UUID | None]
    service_id: Mapped[UUID]
    service_name: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"]
        ),
        ForeignKeyConstraint(["business_id", "lead_id"], ["leads.business_id", "leads.id"]),
        ForeignKeyConstraint(
            ["business_id", "service_id"], ["services.business_id", "services.id"]
        ),
        UniqueConstraint("business_id", "request_id"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 1440"),
        CheckConstraint("status IN ('scheduled','confirmed','completed','cancelled','no_show')"),
    )
