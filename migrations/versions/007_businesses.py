"""Businesses with immutable IDs/subdomain slugs and their initial admin accounts."""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"


def timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "businesses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("plan", sa.String(20)),
        *timestamps(),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "business_admins",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("business_id"),
    )


def downgrade():
    op.drop_table("business_admins")
    op.drop_table("businesses")
