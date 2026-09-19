"""Encrypted, business-owned Gemini API keys."""

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"


def upgrade():
    op.create_table(
        "business_gemini_credentials",
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("key_last_four", sa.String(4), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("business_gemini_credentials")
