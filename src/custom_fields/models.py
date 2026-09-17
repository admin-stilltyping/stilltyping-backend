from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class FieldDefinition(TimestampMixin, Base):
    __tablename__ = "custom_field_definitions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    entity_type: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120))
    field_type: Mapped[str] = mapped_column(String(20))
    options: Mapped[list | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("business_id", "entity_type", "key", name="uq_custom_field_key"),
        CheckConstraint("entity_type IN ('product','service')", name="ck_field_entity"),
        CheckConstraint(
            "field_type IN ('text','number','boolean','select','multiselect','date')",
            name="ck_field_type",
        ),
    )
