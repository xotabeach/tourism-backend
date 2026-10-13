"""A moderator's or editor's «горный маршрут» choice (BACKEND-39)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0091_route_mountain"
down_revision: str | Sequence[str] | None = "0090_notification_anchor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("mountain_manual", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("routes", "mountain_manual")
