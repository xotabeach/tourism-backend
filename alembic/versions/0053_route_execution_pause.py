"""Pause/resume a route execution.

Widens route_executions.status and route_execution_events.action to allow
'paused'/'pause'/'resume', and adds pause-duration tracking so elapsed time
in a completion summary can exclude time spent paused.

Both original CHECK constraints were created inside op.create_table() with an
already-prefixed literal `name=` (a pre-existing bug in 0038/0043): the
naming convention then prefixed it a *second* time, so the real names on
disk are doubled (`ck_route_executions_ck_route_executions_status`,
`ck_route_execution_events_ck_route_execution_events_action`) rather than the
single-prefixed names the convention would normally produce. This migration
drops whichever name is actually present (`IF EXISTS` on both the doubled and
the correct form) rather than hardcoding one, since a downgrade run here also
"fixes" the name going forward — hardcoding either would break the other
direction of an upgrade/downgrade/upgrade cycle. create_check_constraint()
below is always given a short logical name, so the constraints this
migration creates are correctly named exactly once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_route_execution_pause"
down_revision: str | Sequence[str] | None = "0052_content_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_NAMES = (
    "ck_route_executions_ck_route_executions_status",
    "ck_route_executions_status",
)
_ACTION_NAMES = (
    "ck_route_execution_events_ck_route_execution_events_action",
    "ck_route_execution_events_action",
)


def _drop_check_if_exists(table: str, names: Sequence[str]) -> None:
    for name in names:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"')


def upgrade() -> None:
    op.add_column(
        "route_executions",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "route_executions",
        sa.Column(
            "paused_duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    _drop_check_if_exists("route_executions", _STATUS_NAMES)
    op.create_check_constraint(
        "status",
        "route_executions",
        "status IN ('active', 'paused', 'completed', 'cancelled')",
    )

    _drop_check_if_exists("route_execution_events", _ACTION_NAMES)
    op.create_check_constraint(
        "action",
        "route_execution_events",
        "action IN ('complete_stop', 'complete', 'cancel', 'pause', 'resume')",
    )


def downgrade() -> None:
    _drop_check_if_exists("route_execution_events", _ACTION_NAMES)
    op.execute("DELETE FROM route_execution_events WHERE action IN ('pause', 'resume')")
    op.create_check_constraint(
        "action",
        "route_execution_events",
        "action IN ('complete_stop', 'complete', 'cancel')",
    )

    _drop_check_if_exists("route_executions", _STATUS_NAMES)
    op.execute("UPDATE route_executions SET status = 'active' WHERE status = 'paused'")
    op.create_check_constraint(
        "status",
        "route_executions",
        "status IN ('active', 'completed', 'cancelled')",
    )
    op.drop_column("route_executions", "paused_duration_seconds")
    op.drop_column("route_executions", "paused_at")
