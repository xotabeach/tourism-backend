"""APK download counters and active users per app version."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_app_stats"
down_revision: str | Sequence[str] | None = "0062_notifications_created_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apk_download_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("apk_version", sa.String(32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("day", "source", "apk_version", name="pk_apk_download_daily"),
    )
    op.create_table(
        "app_version_daily_users",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=False),
        sa.Column("build_number", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_app_version_daily_users_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "day",
            "app_version",
            "build_number",
            "platform",
            name="pk_app_version_daily_users",
        ),
    )
    op.create_index("ix_app_version_daily_users_day", "app_version_daily_users", ["day"])
    op.create_table(
        "app_version_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=False),
        sa.Column("build_number", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("users", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint(
            "day", "app_version", "build_number", "platform", name="pk_app_version_daily"
        ),
    )


def downgrade() -> None:
    op.drop_table("app_version_daily")
    op.drop_index("ix_app_version_daily_users_day", table_name="app_version_daily_users")
    op.drop_table("app_version_daily_users")
    op.drop_table("apk_download_daily")
