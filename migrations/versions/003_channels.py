"""Messaging channels: per-tenant account routing and webhook idempotency."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003"
down_revision = "002"


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
        "channel_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("account_id", sa.String(200), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("channel", "account_id"),
    )
    op.create_index("ix_channel_accounts_tenant", "channel_accounts", ["tenant_id"])
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("channel", "event_id"),
    )


def downgrade():
    op.drop_table("webhook_events")
    op.drop_table("channel_accounts")
