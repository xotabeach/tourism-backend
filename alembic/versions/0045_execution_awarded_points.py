"""Record travel points granted for a completed route execution."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_execution_awarded_points"
down_revision: str | Sequence[str] | None = "0044_route_terrain_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_executions",
        sa.Column(
            "awarded_points",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_route_executions_awarded_points_non_negative",
        "route_executions",
        "awarded_points >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_route_executions_awarded_points_non_negative",
        "route_executions",
        type_="check",
    )
    op.drop_column("route_executions", "awarded_points")
