from datetime import date
from uuid import UUID

from sqlalchemy import JSON, Date, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class DashboardConfig(TimestampMixin, Base):
    __tablename__ = "dashboard_configs"

    business_id: Mapped[UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    use_case: Mapped[str] = mapped_column(String(30))
    widget_ids: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    demo_seed: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
