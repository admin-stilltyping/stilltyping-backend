"""Add owner-scoped webhook outcomes while retaining old deduplication claims."""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"


def upgrade():
    for column in [
        sa.Column("tenant_id", sa.String(200)),
        sa.Column("external_event_id", sa.String(512)),
        sa.Column("status", sa.String(20), nullable=False, server_default="legacy"),
        sa.Column("deliveries", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_received_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("request_id", sa.Uuid()),
    ]:
        op.add_column("webhook_events", column)
    op.create_index(
        "ix_webhook_events_tenant_created", "webhook_events", ["tenant_id", "created_at", "id"]
    )


def downgrade():
    op.drop_index("ix_webhook_events_tenant_created", table_name="webhook_events")
    for name in [
        "request_id",
        "error_code",
        "duration_ms",
        "completed_at",
        "last_received_at",
        "deliveries",
        "status",
        "external_event_id",
        "tenant_id",
    ]:
        op.drop_column("webhook_events", name)
