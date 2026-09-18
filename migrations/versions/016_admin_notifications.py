"""Business admin notifications, browser subscriptions and durable delivery jobs."""

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"


def upgrade():
    op.create_table(
        "business_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("message", sa.String(200), nullable=False),
        sa.Column("url", sa.String(250), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_notifications_business_created", "business_notifications", ["business_id", "created_at"]
    )
    op.create_table(
        "admin_push_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "admin_id",
            sa.Uuid(),
            sa.ForeignKey("business_admins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("endpoint_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(100), nullable=False),
        sa.Column("auth", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_push_subscriptions_owner", "admin_push_subscriptions", ["business_id", "admin_id"]
    )
    op.create_table(
        "admin_push_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "notification_id",
            sa.Uuid(),
            sa.ForeignKey("business_notifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.Uuid(),
            sa.ForeignKey("admin_push_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("last_error", sa.String(30)),
        sa.UniqueConstraint("notification_id", "subscription_id"),
    )
    op.create_index(
        "ix_push_delivery_pending", "admin_push_deliveries", ["status", "next_attempt_at"]
    )


def downgrade():
    op.drop_table("admin_push_deliveries")
    op.drop_table("admin_push_subscriptions")
    op.drop_table("business_notifications")
