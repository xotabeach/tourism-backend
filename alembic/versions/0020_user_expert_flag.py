"""Add the public expert-profile marker."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_user_expert_flag"
down_revision: str | Sequence[str] | None = "0019_achievements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_expert", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "is_expert")
