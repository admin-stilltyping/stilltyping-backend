"""Persist per-reply AI usage across all channels."""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"


def upgrade():
    op.create_table(
        "ai_usage_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid()),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("tokens_complete", sa.Boolean(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.CheckConstraint("status IN ('completed','failed','awaiting_send','send_failed')"),
        sa.CheckConstraint("duration_ms >= 0 AND input_tokens >= 0 AND output_tokens >= 0"),
    )
    op.create_index(
        "ix_ai_usage_tenant_started", "ai_usage_records", ["tenant_id", "started_at", "id"]
    )
    op.create_index(
        "ix_ai_usage_tenant_channel_started",
        "ai_usage_records",
        ["tenant_id", "channel", "started_at"],
    )


def downgrade():
    op.drop_table("ai_usage_records")
