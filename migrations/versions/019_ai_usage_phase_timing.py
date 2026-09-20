"""Phase breakdown (queue/db/ai/tool/send) for locating slow AI replies."""

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"


def upgrade():
    for column in [
        sa.Column("queue_ms", sa.Float()),
        sa.Column("db_ms", sa.Float()),
        sa.Column("ai_ms", sa.Float()),
        sa.Column("tool_ms", sa.Float()),
        sa.Column("send_ms", sa.Float()),
    ]:
        op.add_column("ai_usage_records", column)


def downgrade():
    for name in ["send_ms", "tool_ms", "ai_ms", "db_ms", "queue_ms"]:
        op.drop_column("ai_usage_records", name)
