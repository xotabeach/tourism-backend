"""Travel+ denormalized user flag and subscriptions table."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_travel_plus_subscriptions"
down_revision: str | Sequence[str] | None = "0026_place_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "travel_plus_active",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("travel_plus_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("travel_plus_plan", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "travel_plus_plan",
        "users",
        "travel_plus_plan IS NULL OR travel_plus_plan IN ('monthly', 'yearly')",
    )

    op.create_table(
        "travel_plus_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan IN ('monthly', 'yearly')",
            name="ck_travel_plus_subscriptions_plan",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'canceled', 'expired')",
            name="ck_travel_plus_subscriptions_status",
        ),
        sa.CheckConstraint(
            "source IN ('admin', 'mock_checkout')",
            name="ck_travel_plus_subscriptions_source",
        ),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name="ck_travel_plus_subscriptions_ends_after_starts",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["admin_principals.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_travel_plus_subscriptions_user_id",
        "travel_plus_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_travel_plus_subscriptions_status",
        "travel_plus_subscriptions",
        ["status"],
    )
    op.create_index(
        "ix_travel_plus_subscriptions_created_by_principal_id",
        "travel_plus_subscriptions",
        ["created_by_principal_id"],
    )
    op.create_index(
        "ix_travel_plus_subscriptions_user_active",
        "travel_plus_subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_travel_plus_subscriptions_user_active",
        table_name="travel_plus_subscriptions",
    )
    op.drop_index(
        "ix_travel_plus_subscriptions_created_by_principal_id",
        table_name="travel_plus_subscriptions",
    )
    op.drop_index(
        "ix_travel_plus_subscriptions_status",
        table_name="travel_plus_subscriptions",
    )
    op.drop_index(
        "ix_travel_plus_subscriptions_user_id",
        table_name="travel_plus_subscriptions",
    )
    op.drop_table("travel_plus_subscriptions")
    op.drop_constraint("ck_users_travel_plus_plan", "users", type_="check")
    op.drop_column("users", "travel_plus_plan")
    op.drop_column("users", "travel_plus_expires_at")
    op.drop_column("users", "travel_plus_active")
