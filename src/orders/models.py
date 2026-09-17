from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    lead_id: Mapped[UUID | None]
    request_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    currency: Mapped[str] = mapped_column(String(3))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    items: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"]
        ),
        ForeignKeyConstraint(["business_id", "lead_id"], ["leads.business_id", "leads.id"]),
        UniqueConstraint("business_id", "request_id"),
        UniqueConstraint("business_id", "reference"),
        CheckConstraint("total >= 0"),
        CheckConstraint("status IN ('confirmed','fulfilled','cancelled')"),
    )
