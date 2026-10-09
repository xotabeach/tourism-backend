"""How much of a run was walked, fixed when the run ends (BACKEND-36, spec 15 D2, D16)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_run_counted_share"
down_revision: str | Sequence[str] | None = "0082_skip_stop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_executions",
        sa.Column("completed_share_percent", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "route_executions",
        sa.Column("paid_distance_meters", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "completed_share_percent_range",
        "route_executions",
        "completed_share_percent IS NULL OR completed_share_percent BETWEEN 0 AND 100",
    )
    # Every run completed so far was completed without skips: it was walked
    # whole. ``paid_distance_meters`` stays NULL for them and readers fall
    # back to the route's length in the start snapshot.
    op.execute(
        "UPDATE route_executions SET completed_share_percent = 100 "
        "WHERE status = 'completed' AND counted IS TRUE"
    )


def downgrade() -> None:
    op.drop_constraint("completed_share_percent_range", "route_executions", type_="check")
    op.drop_column("route_executions", "paid_distance_meters")
    op.drop_column("route_executions", "completed_share_percent")
