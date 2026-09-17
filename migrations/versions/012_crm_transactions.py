"""Contacts and enquiries; customer conversion through orders and appointments."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"


def base():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("business_id", sa.Uuid(), nullable=False),
    ]


def timestamps():
    return [
        sa.Column(key, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        for key in ("created_at", "updated_at")
    ]


def scoped_fk(key, table):
    return sa.ForeignKeyConstraint(["business_id", key], [f"{table}.business_id", f"{table}.id"])


def request_columns():
    return [
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("business_id", "request_id"),
    ]


def upgrade():
    op.create_table(
        "crm_contacts",
        *base(),
        *timestamps(),
        sa.Column("phone", sa.String(16)),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("business_id", "id"),
        sa.UniqueConstraint("business_id", "phone"),
    )
    op.create_table(
        "crm_social_identities",
        *base(),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(500), nullable=False),
        scoped_fk("contact_id", "crm_contacts"),
        sa.UniqueConstraint("business_id", "platform", "external_id"),
    )
    op.create_table(
        "customers",
        *base(),
        *timestamps(),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        scoped_fk("contact_id", "crm_contacts"),
        sa.UniqueConstraint("business_id", "contact_id"),
        sa.UniqueConstraint("business_id", "id"),
    )
    op.create_table(
        "leads",
        *base(),
        *timestamps(),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid()),
        sa.Column("converted_at", sa.DateTime(timezone=True)),
        scoped_fk("contact_id", "crm_contacts"),
        scoped_fk("customer_id", "customers"),
        sa.UniqueConstraint("business_id", "contact_id"),
        sa.UniqueConstraint("business_id", "id"),
    )
    op.create_table(
        "lead_sources",
        *base(),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(500), nullable=False),
        scoped_fk("lead_id", "leads"),
        sa.UniqueConstraint("business_id", "channel", "external_id"),
    )
    op.create_table(
        "enquiries",
        *base(),
        *timestamps(),
        *request_columns(),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        scoped_fk("lead_id", "leads"),
    )
    op.create_table(
        "services",
        *base(),
        *timestamps(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category", sa.String(100)),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "custom_fields",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("business_id", "id"),
        sa.CheckConstraint("price >= 0"),
        sa.CheckConstraint("duration_minutes BETWEEN 1 AND 1440"),
        sa.CheckConstraint("status IN ('active','inactive','archived')"),
    )
    op.create_table(
        "orders",
        *base(),
        *timestamps(),
        *request_columns(),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid()),
        sa.Column("reference", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "items", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        scoped_fk("customer_id", "customers"),
        scoped_fk("lead_id", "leads"),
        sa.UniqueConstraint("business_id", "reference"),
        sa.CheckConstraint("total >= 0"),
        sa.CheckConstraint("status IN ('confirmed','fulfilled','cancelled')"),
    )
    op.create_table(
        "appointments",
        *base(),
        *timestamps(),
        *request_columns(),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid()),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.String(200), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text()),
        scoped_fk("customer_id", "customers"),
        scoped_fk("lead_id", "leads"),
        scoped_fk("service_id", "services"),
        sa.CheckConstraint("duration_minutes BETWEEN 1 AND 1440"),
        sa.CheckConstraint("status IN ('scheduled','confirmed','completed','cancelled','no_show')"),
    )
    for table in ("customers", "leads", "enquiries", "services", "orders", "appointments"):
        op.create_index(f"ix_{table}_business_created", table, ["business_id", "created_at"])


def downgrade():
    for table in (
        "appointments",
        "orders",
        "services",
        "enquiries",
        "lead_sources",
        "leads",
        "customers",
        "crm_social_identities",
        "crm_contacts",
    ):
        op.drop_table(table)
