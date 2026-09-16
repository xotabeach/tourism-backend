"""Durable SMS Aero delivery jobs for authentication OTP codes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_sms_delivery_jobs"
down_revision: str | Sequence[str] | None = "0058_locality_search_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sms_delivery_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("plaintext_code", sa.String(length=8), nullable=True),
        sa.Column("otp_challenge_id", sa.Uuid(), nullable=True),
        sa.Column("phone_change_challenge_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_sms_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')",
            name="ck_sms_delivery_jobs_status",
        ),
        sa.CheckConstraint(
            "(otp_challenge_id IS NOT NULL AND phone_change_challenge_id IS NULL) OR "
            "(otp_challenge_id IS NULL AND phone_change_challenge_id IS NOT NULL)",
            name="ck_sms_delivery_jobs_exactly_one_challenge",
        ),
        sa.ForeignKeyConstraint(
            ["otp_challenge_id"], ["auth_otp_challenges.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["phone_change_challenge_id"],
            ["auth_phone_change_challenges.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sms_delivery_jobs"),
        sa.UniqueConstraint("otp_challenge_id", name="uq_sms_delivery_jobs_otp_challenge_id"),
        sa.UniqueConstraint(
            "phone_change_challenge_id",
            name="uq_sms_delivery_jobs_phone_change_challenge_id",
        ),
    )
    op.create_index("ix_sms_delivery_jobs_phone_e164", "sms_delivery_jobs", ["phone_e164"])
    op.create_index(
        "ix_sms_delivery_jobs_next_attempt_at", "sms_delivery_jobs", ["next_attempt_at"]
    )
    op.create_index(
        "ix_sms_delivery_jobs_poll",
        "sms_delivery_jobs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.alter_column(
        "runtime_settings",
        "value",
        existing_type=sa.String(length=256),
        type_=sa.String(length=640),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "runtime_settings",
        "value",
        existing_type=sa.String(length=640),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    op.drop_index("ix_sms_delivery_jobs_poll", table_name="sms_delivery_jobs")
    op.drop_index("ix_sms_delivery_jobs_next_attempt_at", table_name="sms_delivery_jobs")
    op.drop_index("ix_sms_delivery_jobs_phone_e164", table_name="sms_delivery_jobs")
    op.drop_table("sms_delivery_jobs")
