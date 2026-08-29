"""Add append-only route execution event ledger for offline replays."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_route_execution_events"
down_revision: str | Sequence[str] | None = "0042_route_recommendations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_execution_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("stop_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("client_event_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "action IN ('complete_stop', 'complete', 'cancel')",
            name="ck_route_execution_events_action",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["route_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stop_id"], ["route_execution_stops.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "client_event_id",
            name="uq_route_execution_events_user_event",
        ),
    )
    op.create_index(
        "ix_route_execution_events_execution_id",
        "route_execution_events",
        ["execution_id"],
    )
    op.create_index(
        "ix_route_execution_events_user_id",
        "route_execution_events",
        ["user_id"],
    )
    op.create_index(
        "ix_route_execution_events_execution_recorded",
        "route_execution_events",
        ["execution_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_execution_events_execution_recorded",
        table_name="route_execution_events",
    )
    op.drop_index(
        "ix_route_execution_events_user_id",
        table_name="route_execution_events",
    )
    op.drop_index(
        "ix_route_execution_events_execution_id",
        table_name="route_execution_events",
    )
    op.drop_table("route_execution_events")
