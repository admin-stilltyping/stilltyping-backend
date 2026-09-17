"""Durable support tickets created on escalation."""

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"


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
        "support_tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("ticket_ref", sa.String(20), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("channel", sa.String(20)),
        sa.Column("external_user_id", sa.String(200)),
        sa.Column("notes", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "request_id"),
        sa.UniqueConstraint("ticket_ref"),
        sa.CheckConstraint("status IN ('open','resolved')"),
    )
    op.create_index(
        "ix_support_tickets_tenant_status", "support_tickets", ["tenant_id", "status"]
    )


def downgrade():
    op.drop_table("support_tickets")
