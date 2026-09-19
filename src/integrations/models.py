from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin


class BusinessGeminiCredential(TimestampMixin, Base):
    __tablename__ = "business_gemini_credentials"

    business_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_key: Mapped[str] = mapped_column(Text)
    key_last_four: Mapped[str] = mapped_column(String(4))
