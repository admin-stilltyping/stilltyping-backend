from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from context_agent.db import Base, TimestampMixin
from super_admin.businesses.models import Business


class Contact(TimestampMixin, Base):
    __tablename__ = "crm_contacts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID] = mapped_column(ForeignKey(Business.id, ondelete="RESTRICT"))
    phone: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("business_id", "id"),
        UniqueConstraint("business_id", "phone"),
    )


class SocialIdentity(Base):
    __tablename__ = "crm_social_identities"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    platform: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "contact_id"], ["crm_contacts.business_id", "crm_contacts.id"]
        ),
        UniqueConstraint("business_id", "platform", "external_id"),
    )


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "contact_id"], ["crm_contacts.business_id", "crm_contacts.id"]
        ),
        UniqueConstraint("business_id", "contact_id"),
        UniqueConstraint("business_id", "id"),
    )


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    customer_id: Mapped[UUID | None]
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "contact_id"], ["crm_contacts.business_id", "crm_contacts.id"]
        ),
        ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"]
        ),
        UniqueConstraint("business_id", "contact_id"),
        UniqueConstraint("business_id", "id"),
    )


class LeadSource(Base):
    __tablename__ = "lead_sources"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    lead_id: Mapped[UUID]
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(500))
    __table_args__ = (
        ForeignKeyConstraint(["business_id", "lead_id"], ["leads.business_id", "leads.id"]),
        UniqueConstraint("business_id", "channel", "external_id"),
    )


class Enquiry(TimestampMixin, Base):
    __tablename__ = "enquiries"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    business_id: Mapped[UUID]
    lead_id: Mapped[UUID]
    request_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["business_id", "lead_id"], ["leads.business_id", "leads.id"]),
        UniqueConstraint("business_id", "request_id"),
    )
