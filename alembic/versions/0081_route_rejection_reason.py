"""Why a route was returned to its author (BACKEND-34, spec 15 D6)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_route_rejection_reason"
down_revision: str | Sequence[str] | None = "0080_popularity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("rejection_reason", sa.String(length=32), nullable=True))
    op.add_column("routes", sa.Column("moderator_note", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("routes", "moderator_note")
    op.drop_column("routes", "rejection_reason")
