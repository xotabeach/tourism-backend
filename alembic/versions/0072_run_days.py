"""Night pauses and early finish of multi-day runs (BACKEND-24, spec 14a section 5)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072_run_days"
down_revision: str | Sequence[str] | None = "0071_manual_day_breaks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION_NAMES = (
    "ck_route_execution_events_ck_route_execution_events_action",
    "ck_route_execution_events_action",
)
_ACTIONS_BEFORE = "'complete_stop', 'uncomplete_stop', 'complete', 'cancel', 'pause', 'resume'"


def _swap_actions(actions: str) -> None:
    for name in _ACTION_NAMES:
        op.execute(f'ALTER TABLE route_execution_events DROP CONSTRAINT IF EXISTS "{name}"')
    op.create_check_constraint("action", "route_execution_events", f"action IN ({actions})")


def upgrade() -> None:
    op.add_column(
        "route_executions",
        sa.Column("night_pauses", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "route_executions",
        sa.Column("night_paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # «Завершить многодневный» and the idle close are cancellations that
    # still pay for the finished days; old apps know «cancelled» already.
    op.add_column(
        "route_executions",
        sa.Column("ended_early", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _swap_actions(_ACTIONS_BEFORE + ", 'end_day', 'finish_early'")


def downgrade() -> None:
    op.execute("DELETE FROM route_execution_events WHERE action IN ('end_day', 'finish_early')")
    _swap_actions(_ACTIONS_BEFORE)
    for column in ("ended_early", "night_paused", "night_pauses"):
        op.drop_column("route_executions", column)
