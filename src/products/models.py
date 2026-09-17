from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="active")
    attributes: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    __table_args__ = (
        UniqueConstraint("business_id", "sku", name="uq_product_business_sku"),
        CheckConstraint("price >= 0", name="ck_product_price"),
        CheckConstraint(
            "status IN ('active','inactive','out_of_stock','archived')", name="ck_product_status"
        ),
        Index("ix_products_business_status", "business_id", "status"),
    )
