"""Review media attachments and expert-status audit history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_review_media_expert_audit"
down_revision: str | Sequence[str] | None = "0021_support_reply_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_media_attachments_entity_type",
        "media_attachments",
        type_="check",
    )
    op.create_check_constraint(
        "entity_type",
        "media_attachments",
        "entity_type IN ('user', 'place', 'route', 'review')",
    )

    op.create_table(
        "user_expert_status_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("is_expert", sa.Boolean(), nullable=False),
        sa.Column("changed_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["changed_by_principal_id"],
            ["admin_principals.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_expert_status_events_user_id",
        "user_expert_status_events",
        ["user_id"],
    )
    op.create_index(
        "ix_user_expert_status_events_changed_by_principal_id",
        "user_expert_status_events",
        ["changed_by_principal_id"],
    )
    op.execute(
        """
        INSERT INTO user_expert_status_events (
            id, user_id, is_expert, changed_by_principal_id, changed_at
        )
        SELECT gen_random_uuid(), id, true, NULL, COALESCE(updated_at, now())
        FROM users
        WHERE is_expert IS TRUE
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_expert_status_events_changed_by_principal_id",
        table_name="user_expert_status_events",
    )
    op.drop_index(
        "ix_user_expert_status_events_user_id",
        table_name="user_expert_status_events",
    )
    op.drop_table("user_expert_status_events")

    op.execute("DELETE FROM media_attachments WHERE entity_type = 'review'")
    op.drop_constraint(
        "ck_media_attachments_entity_type",
        "media_attachments",
        type_="check",
    )
    op.create_check_constraint(
        "entity_type",
        "media_attachments",
        "entity_type IN ('user', 'place', 'route')",
    )
