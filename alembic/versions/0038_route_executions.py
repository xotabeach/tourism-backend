"""Add route execution v0 state and stop snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_route_executions"
down_revision: str | Sequence[str] | None = "0037_expert_travel_rank"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("route_name", sa.String(length=255), nullable=False),
        sa.Column("route_cover_url", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'cancelled')",
            name="ck_route_executions_status",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_route_executions_route_id", "route_executions", ["route_id"])
    op.create_index("ix_route_executions_status", "route_executions", ["status"])
    op.create_index("ix_route_executions_user_id", "route_executions", ["user_id"])
    op.create_index(
        "ix_route_executions_user_started",
        "route_executions",
        ["user_id", "started_at"],
    )
    op.create_index(
        "uq_route_executions_one_active_per_user",
        "route_executions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "route_execution_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("route_stop_id", sa.Uuid(), nullable=True),
        sa.Column("place_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("place_name", sa.String(length=255), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("is_optional", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 1", name="ck_route_execution_stops_position_positive"),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["route_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["route_stop_id"], ["route_stops.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "position",
            name="uq_route_execution_stops_execution_position",
        ),
    )
    op.create_index(
        "ix_route_execution_stops_execution_id",
        "route_execution_stops",
        ["execution_id"],
    )
    op.create_index(
        "ix_route_execution_stops_execution_position",
        "route_execution_stops",
        ["execution_id", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_execution_stops_execution_position",
        table_name="route_execution_stops",
    )
    op.drop_index("ix_route_execution_stops_execution_id", table_name="route_execution_stops")
    op.drop_table("route_execution_stops")
    op.drop_index("uq_route_executions_one_active_per_user", table_name="route_executions")
    op.drop_index("ix_route_executions_user_started", table_name="route_executions")
    op.drop_index("ix_route_executions_user_id", table_name="route_executions")
    op.drop_index("ix_route_executions_status", table_name="route_executions")
    op.drop_index("ix_route_executions_route_id", table_name="route_executions")
    op.drop_table("route_executions")
