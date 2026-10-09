"""Track sender attribution and demo reset cutoffs without storing message text."""

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"


def upgrade():
    op.add_column("webhook_events", sa.Column("external_user_id", sa.String(200)))
    op.create_index(
        "ix_webhook_events_sender",
        "webhook_events",
        ["tenant_id", "channel", "external_user_id"],
    )
    op.create_table(
        "instagram_demo_resets",
        sa.Column("tenant_id", sa.String(200), primary_key=True),
        sa.Column("external_user_id", sa.String(200), primary_key=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("instagram_demo_resets")
    op.drop_index("ix_webhook_events_sender", table_name="webhook_events")
    op.drop_column("webhook_events", "external_user_id")
