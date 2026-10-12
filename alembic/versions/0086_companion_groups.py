"""Groups of companions and their invitations (BACKEND-59, spec 11b)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086_companion_groups"
down_revision: str | Sequence[str] | None = "0085_user_blocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BEFORE = (
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked', "
    "'article_published', 'article_rejected', "
    "'article_comment', 'article_about_your_route', "
    "'antifraud_flagged', 'antifraud_blocked', 'antifraud_points_decision'"
)
_COMPANION_KINDS = (
    "'companion_invite', 'companion_join_request', 'companion_group_cancelled', "
    "'companion_group_started', 'companion_member_finished', 'companion_member_left'"
)
_TARGETS_BEFORE = "'route', 'user', 'achievement', 'support_ticket', 'article'"
_REPORTS_BEFORE = "'article_comment', 'article', 'route', 'place'"
_REPORT_CHECK = "ck_content_reports_ck_content_reports_target"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "companion_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "route_id", sa.Uuid(), sa.ForeignKey("routes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "organizer_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("max_members", sa.Integer(), nullable=False),
        sa.Column("join_code", sa.String(8), nullable=False),
        sa.Column("route_version_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('gathering', 'started', 'settled', 'cancelled')",
            name="status",
        ),
        sa.CheckConstraint("max_members BETWEEN 2 AND 8", name="max_members"),
    )
    op.create_index(
        "uq_companion_groups_live_code",
        "companion_groups",
        ["join_code"],
        unique=True,
        postgresql_where=sa.text("status IN ('gathering', 'started')"),
    )
    op.create_index("ix_companion_groups_route", "companion_groups", ["route_id"])
    op.create_index(
        "ix_companion_groups_organizer_user_id", "companion_groups", ["organizer_user_id"]
    )

    op.create_table(
        "companion_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("companion_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "invited_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "execution_id",
            sa.Uuid(),
            sa.ForeignKey("route_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("joined_late", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("hidden_from_card", sa.Boolean(), nullable=False, server_default="false"),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('invited', 'requested', 'joined', 'declined', 'revoked', "
            "'rejected', 'removed', 'left')",
            name="state",
        ),
        sa.UniqueConstraint("group_id", "user_id", name="uq_companion_members_group_user"),
    )
    op.create_index("ix_companion_members_user_state", "companion_members", ["user_id", "state"])
    op.create_index(
        "ix_companion_members_inviter_created",
        "companion_members",
        ["invited_by_user_id", "created_at"],
    )

    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint(
        "kind", "notifications", f"kind IN ({_KINDS_BEFORE}, {_COMPANION_KINDS})"
    )
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint(
        "target_type",
        "notifications",
        f"target_type IS NULL OR target_type IN ({_TARGETS_BEFORE}, 'companion_group')",
    )
    # 0052 named it in full, so the naming convention doubled the prefix.
    op.drop_constraint(op.f(_REPORT_CHECK), "content_reports", type_="check")
    op.create_check_constraint(
        "ck_content_reports_target",
        "content_reports",
        f"target_type IN ({_REPORTS_BEFORE}, 'companion_group')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM content_reports WHERE target_type = 'companion_group'")
    op.drop_constraint(op.f(_REPORT_CHECK), "content_reports", type_="check")
    op.create_check_constraint(
        "ck_content_reports_target", "content_reports", f"target_type IN ({_REPORTS_BEFORE})"
    )
    op.execute(
        f"DELETE FROM notifications WHERE kind IN ({_COMPANION_KINDS}) "
        "OR target_type = 'companion_group'"
    )
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint(
        "target_type", "notifications", f"target_type IS NULL OR target_type IN ({_TARGETS_BEFORE})"
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", f"kind IN ({_KINDS_BEFORE})")
    op.drop_table("companion_members")
    op.drop_table("companion_groups")
