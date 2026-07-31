"""Per-user notification preference flags on users."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_user_notification_prefs"
down_revision: str | Sequence[str] | None = "0008_otp_debug_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notify_push_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notify_sms_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notify_haptics_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_haptics_enabled")
    op.drop_column("users", "notify_sms_enabled")
    op.drop_column("users", "notify_push_enabled")
