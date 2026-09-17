"""Persist module selection; retain existing portal access for existing businesses."""

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"


def upgrade():
    op.create_table(
        "business_modules",
        sa.Column(
            "business_id",
            sa.Uuid(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *[
            sa.Column(key, sa.Boolean(), nullable=False)
            for key in (
                "product_orders",
                "service_appointments",
                "customers",
                "leads",
                "support_tickets",
            )
        ],
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("super_admins.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "customers OR (NOT product_orders AND NOT service_appointments)",
            name="ck_module_customers_dependency",
        ),
        sa.CheckConstraint("revision > 0", name="ck_module_revision"),
    )
    # No existing businesses lose their currently visible modules during rollout.
    op.execute(
        sa.text("""INSERT INTO business_modules
        (business_id, product_orders, service_appointments, customers, leads, support_tickets, revision)
        SELECT id, true, true, true, false, true, 1 FROM businesses""")
    )


def downgrade():
    op.drop_table("business_modules")
