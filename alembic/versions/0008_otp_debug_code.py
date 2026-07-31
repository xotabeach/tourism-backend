"""Readable OTP code for local/test while no SMS provider is connected."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_otp_debug_code"
down_revision: str | Sequence[str] | None = "0007_media_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auth_otp_challenges",
        sa.Column("debug_code", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "auth_phone_change_challenges",
        sa.Column("debug_code", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auth_phone_change_challenges", "debug_code")
    op.drop_column("auth_otp_challenges", "debug_code")
