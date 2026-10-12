"""Hats for a companion's character (BACKEND-65)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089_bean_hats"
down_revision: str | Sequence[str] | None = "0088_route_coauthors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bean_hat", sa.String(32), nullable=True))
    op.create_table(
        "user_bean_hats",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("hat_slug", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_bean_hats")
    op.drop_column("users", "bean_hat")
