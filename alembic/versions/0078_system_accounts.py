"""Service accounts such as the КРЫМТРИП editorial profile (BACKEND-16, spec 16 D6, D18)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078_system_accounts"
down_revision: str | Sequence[str] | None = "0077_admin_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_system_account", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_system_account")
