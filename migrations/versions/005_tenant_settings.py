"""Per-tenant agent instructions (business prompt)."""

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"


def upgrade():
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.String(200), primary_key=True),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("tenant_settings")
