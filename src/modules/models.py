from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin
from super_admin.businesses.models import Business
from super_admin.models import SuperAdmin


class BusinessModules(TimestampMixin, Base):
    __tablename__ = "business_modules"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey(Business.id, ondelete="CASCADE"), primary_key=True
    )
    product_orders: Mapped[bool] = mapped_column(Boolean, default=False)
    service_appointments: Mapped[bool] = mapped_column(Boolean, default=False)
    customers: Mapped[bool] = mapped_column(Boolean, default=False)
    leads: Mapped[bool] = mapped_column(Boolean, default=False)
    support_tickets: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[UUID | None] = mapped_column(
        ForeignKey(SuperAdmin.id, ondelete="SET NULL")
    )
    __table_args__ = (
        CheckConstraint(
            "customers OR (NOT product_orders AND NOT service_appointments)",
            name="ck_module_customers_dependency",
        ),
        CheckConstraint("revision > 0", name="ck_module_revision"),
    )
