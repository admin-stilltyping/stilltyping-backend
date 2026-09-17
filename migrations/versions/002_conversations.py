"""Conversation history: stored input and output messages."""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"


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
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "channel", "external_id"),
    )
    op.create_index("ix_conversations_tenant", "conversations", ["tenant_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user','assistant')"),
        sa.UniqueConstraint("conversation_id", "seq"),
    )
    op.create_index("ix_messages_conversation_seq", "messages", ["conversation_id", "seq"])


def downgrade():
    op.drop_table("messages")
    op.drop_table("conversations")
