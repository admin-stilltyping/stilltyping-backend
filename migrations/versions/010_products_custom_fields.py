"""Tenant-specific product catalog and custom field definitions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "010"
down_revision = "009"


def timestamps():
    return [
        sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        for name in ("created_at", "updated_at")
    ]


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "custom_field_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("field_type", sa.String(20), nullable=False),
        sa.Column("options", json_type, nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("business_id", "entity_type", "key", name="uq_custom_field_key"),
        sa.CheckConstraint("entity_type IN ('product','service')", name="ck_field_entity"),
        sa.CheckConstraint(
            "field_type IN ('text','number','boolean','select','multiselect','date')",
            name="ck_field_type",
        ),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sku", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attributes", json_type, nullable=False),
        *timestamps(),
        sa.UniqueConstraint("business_id", "sku", name="uq_product_business_sku"),
        sa.CheckConstraint("price >= 0", name="ck_product_price"),
        sa.CheckConstraint(
            "status IN ('active','inactive','out_of_stock','archived')", name="ck_product_status"
        ),
    )
    op.create_index("ix_products_business_status", "products", ["business_id", "status"])


def downgrade():
    op.drop_table("products")
    op.drop_table("custom_field_definitions")
