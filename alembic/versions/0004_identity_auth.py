"""Identity users, OTP challenges, refresh sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_identity_auth"
down_revision: str | Sequence[str] | None = "0003_editorial_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("privacy_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("personal_data_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("phone_e164", name="uq_users_phone_e164"),
    )
    op.create_index("ix_users_phone_e164", "users", ["phone_e164"])

    op.create_table(
        "auth_otp_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_otp_challenges_phone", "auth_otp_challenges", ["phone_e164"])
    op.create_index("ix_auth_otp_challenges_expires", "auth_otp_challenges", ["expires_at"])

    op.create_table(
        "auth_refresh_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("device_label", sa.String(length=120), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_digest", name="uq_auth_refresh_sessions_token_digest"),
    )
    op.create_index("ix_auth_refresh_sessions_user_id", "auth_refresh_sessions", ["user_id"])
    op.create_index("ix_auth_refresh_sessions_family_id", "auth_refresh_sessions", ["family_id"])


def downgrade() -> None:
    op.drop_table("auth_refresh_sessions")
    op.drop_table("auth_otp_challenges")
    op.drop_table("users")
