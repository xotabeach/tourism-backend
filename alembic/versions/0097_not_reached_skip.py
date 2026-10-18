"""«not_reached»: stops left open when a run is finished early (BACKEND-75)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0097_not_reached_skip"
down_revision: str | Sequence[str] | None = "0096_app_error_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMES = (
    "ck_route_execution_stops_ck_route_execution_stops_skip_reason",
    "ck_route_execution_stops_skip_reason",
)
_BEFORE = "'closed', 'no_time', 'hard', 'other'"


def _swap(reasons: str) -> None:
    for name in _NAMES:
        op.execute(f'ALTER TABLE route_execution_stops DROP CONSTRAINT IF EXISTS "{name}"')
    op.create_check_constraint(
        "skip_reason",
        "route_execution_stops",
        f"skip_reason IS NULL OR skip_reason IN ({reasons})",
    )


def upgrade() -> None:
    _swap(_BEFORE + ", 'not_reached'")


def downgrade() -> None:
    # Back to the walker's reasons: a stop the server closed as not reached
    # becomes «other», the nearest of them.
    op.execute(
        "UPDATE route_execution_stops SET skip_reason = 'other' WHERE skip_reason = 'not_reached'"
    )
    _swap(_BEFORE)
