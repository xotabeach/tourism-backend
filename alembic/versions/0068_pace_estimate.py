"""Separate the pace-check estimate from the displayed leg (BACKEND-12, spec 12a D4)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_pace_estimate"
down_revision: str | Sequence[str] | None = "0067_achievement_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_execution_stops",
        sa.Column("pace_estimate_seconds", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_execution_stops", "pace_estimate_seconds")
