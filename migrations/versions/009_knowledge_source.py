"""Keep the original knowledge text for the business editor."""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"


def upgrade():
    op.add_column("documents", sa.Column("source_text", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("documents", "source_text")
