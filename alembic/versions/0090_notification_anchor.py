"""The comment or review a notification is about (BACKEND-67)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090_notification_anchor"
down_revision: str | Sequence[str] | None = "0089_bean_hats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("anchor_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "anchor_id")
