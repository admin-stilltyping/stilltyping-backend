"""Initial agreed three-table schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None


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
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    op.create_table(
        "knowledge_units",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding_status", sa.String(10), nullable=False, server_default="pending"),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("embedding_status IN ('pending','ready','failed')"),
    )
    op.create_index("ix_units_tenant_document", "knowledge_units", ["tenant_id", "document_id"])
    op.create_table(
        "tools",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameters_schema", postgresql.JSONB(), nullable=False),
        sa.Column("handler_key", sa.String(200), nullable=False),
        sa.Column("embedding_status", sa.String(10), nullable=False, server_default="pending"),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", "name"),
        sa.CheckConstraint("embedding_status IN ('pending','ready','failed')"),
    )
    op.execute(
        "CREATE INDEX ix_knowledge_fts ON knowledge_units USING gin "
        "(to_tsvector('simple', title || ' ' || content))"
    )
    op.execute(
        "CREATE INDEX ix_tools_fts ON tools USING gin "
        "(to_tsvector('simple', name || ' ' || description))"
    )


def downgrade():
    op.drop_table("tools")
    op.drop_table("knowledge_units")
    op.drop_table("documents")
