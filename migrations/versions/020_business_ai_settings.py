"""Per-business AI providers and isolated embedding indexes."""

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"


def upgrade():
    op.create_table(
        "business_ai_settings",
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("index_prefix", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("business_ai_settings")
