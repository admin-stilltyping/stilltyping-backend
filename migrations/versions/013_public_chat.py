"""Private visitor sessions and retry-safe public web chat turns."""

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"


def timestamps():
    return [
        sa.Column(key, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        for key in ("created_at", "updated_at")
    ]


def upgrade():
    op.create_table(
        "web_chat_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_web_chat_sessions_business", "web_chat_sessions", ["business_id"])
    op.create_table(
        "web_chat_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text()),
        sa.Column("status", sa.String(12), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["session_id"], ["web_chat_sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("session_id", "request_id"),
        sa.UniqueConstraint("session_id", "seq"),
        sa.CheckConstraint("status IN ('pending','complete','failed')"),
    )
    op.create_index(
        "ix_web_chat_turns_session_created", "web_chat_turns", ["session_id", "created_at"]
    )


def downgrade():
    op.drop_table("web_chat_turns")
    op.drop_table("web_chat_sessions")
