"""Add confirmed_fields to route planning sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_session_confirmed_fields"
down_revision: str | Sequence[str] | None = "0030_route_planning_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_planning_sessions",
        sa.Column(
            "confirmed_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("route_planning_sessions", "confirmed_fields")
