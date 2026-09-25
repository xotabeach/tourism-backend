"""«Легче / как ожидал / сложнее» after a run (BACKEND-17, spec 17, section 7)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076_difficulty_feedback"
down_revision: str | Sequence[str] | None = "0075_route_difficulty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_executions", sa.Column("difficulty_feedback", sa.String(12), nullable=True)
    )
    op.add_column(
        "route_executions",
        sa.Column("difficulty_feedback_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_route_executions_difficulty_feedback",
        "route_executions",
        "difficulty_feedback IS NULL OR difficulty_feedback IN ('easier', 'as_expected', 'harder')",
    )


def downgrade() -> None:
    op.drop_column("route_executions", "difficulty_feedback_at")
    op.drop_column("route_executions", "difficulty_feedback")
