"""Skipping a stop of a run and whether the run counts (BACKEND-35, spec 15 D1, D4, D14)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082_skip_stop"
down_revision: str | Sequence[str] | None = "0081_route_rejection_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION_NAMES = (
    "ck_route_execution_events_ck_route_execution_events_action",
    "ck_route_execution_events_action",
)
_ACTIONS_BEFORE = (
    "'complete_stop', 'uncomplete_stop', 'complete', 'cancel', 'pause', "
    "'resume', 'end_day', 'finish_early'"
)


def _swap_actions(actions: str) -> None:
    for name in _ACTION_NAMES:
        op.execute(f'ALTER TABLE route_execution_events DROP CONSTRAINT IF EXISTS "{name}"')
    op.create_check_constraint("action", "route_execution_events", f"action IN ({actions})")


def upgrade() -> None:
    op.add_column(
        "route_execution_stops",
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "route_execution_stops",
        sa.Column("skip_reason", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "skip_reason",
        "route_execution_stops",
        "skip_reason IS NULL OR skip_reason IN ('closed', 'no_time', 'hard', 'other')",
    )
    # A stop is marked or skipped, never both; a skip always has its reason.
    op.create_check_constraint(
        "skip_state",
        "route_execution_stops",
        "(skipped_at IS NULL AND skip_reason IS NULL) "
        "OR (skipped_at IS NOT NULL AND skip_reason IS NOT NULL AND completed_at IS NULL)",
    )
    # Every run finished so far was finished in full, so it counts.
    op.add_column(
        "route_executions",
        sa.Column("counted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    _swap_actions(_ACTIONS_BEFORE + ", 'skip_stop', 'unskip_stop'")


def downgrade() -> None:
    op.execute("DELETE FROM route_execution_events WHERE action IN ('skip_stop', 'unskip_stop')")
    _swap_actions(_ACTIONS_BEFORE)
    op.drop_column("route_executions", "counted")
    op.drop_constraint("skip_state", "route_execution_stops", type_="check")
    op.drop_constraint("skip_reason", "route_execution_stops", type_="check")
    op.drop_column("route_execution_stops", "skip_reason")
    op.drop_column("route_execution_stops", "skipped_at")
