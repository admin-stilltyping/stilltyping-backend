from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin
from super_admin.businesses.models import Business


class Service(TimestampMixin, Base):
    __tablename__ = "services"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey(Business.id, ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(20), default="active")
    custom_fields: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    __table_args__ = (
        UniqueConstraint("business_id", "id"),
        CheckConstraint("price >= 0"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 1440"),
        CheckConstraint("status IN ('active','inactive','archived')"),
    )
