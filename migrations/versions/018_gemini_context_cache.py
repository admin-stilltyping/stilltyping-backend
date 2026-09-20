"""Daytime Gemini caches and provider-reported cache usage."""

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"


def upgrade():
    op.create_table(
        "gemini_context_caches",
        sa.Column("tenant_id", sa.String(200), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("cache_name", sa.String(300)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("token_count", sa.Integer()),
        sa.Column("lease_owner", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
    )
    # Historical usage did not record cache reads: NULL means unknown, not zero.
    op.add_column("ai_usage_records", sa.Column("cached_input_tokens", sa.Integer()))


def downgrade():
    op.drop_column("ai_usage_records", "cached_input_tokens")
    op.drop_table("gemini_context_caches")
