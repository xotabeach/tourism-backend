"""Route execution events: «uncomplete_stop» (FRONTEND-36).

The last marked stop of a run in progress can be unmarked again; the event
journal records it like every other mutation.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0066_uncomplete_stop"
down_revision: str | Sequence[str] | None = "0065_route_seaside_tag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ACTIONS = "action IN ('complete_stop', 'complete', 'cancel', 'pause', 'resume')"
_NEW_ACTIONS = (
    "action IN ('complete_stop', 'uncomplete_stop', 'complete', 'cancel', 'pause', 'resume')"
)


def upgrade() -> None:
    op.drop_constraint("action", "route_execution_events", type_="check")
    op.create_check_constraint("action", "route_execution_events", _NEW_ACTIONS)


def downgrade() -> None:
    op.execute("DELETE FROM route_execution_events WHERE action = 'uncomplete_stop'")
    op.drop_constraint("action", "route_execution_events", type_="check")
    op.create_check_constraint("action", "route_execution_events", _OLD_ACTIONS)
