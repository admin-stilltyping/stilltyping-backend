"""Super-admin accounts with password hashes, token revocation, and login throttling."""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"


def upgrade():
    op.create_table(
        "super_admins",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("username"),
    )


def downgrade():
    op.drop_table("super_admins")
