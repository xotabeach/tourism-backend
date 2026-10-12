"""Co-authors of a user route, consent to publish (BACKEND-62, spec 11c D7)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088_route_coauthors"
down_revision: str | Sequence[str] | None = "0087_company_achievement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BEFORE = (
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked', "
    "'article_published', 'article_rejected', "
    "'article_comment', 'article_about_your_route', "
    "'antifraud_flagged', 'antifraud_blocked', 'antifraud_points_decision', "
    "'companion_invite', 'companion_join_request', 'companion_group_cancelled', "
    "'companion_group_started', 'companion_member_finished', 'companion_member_left'"
)
_COAUTHOR_KINDS = "'route_coauthor_invite', 'route_coauthor_declined'"


def upgrade() -> None:
    op.create_table(
        "route_coauthors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "route_id", sa.Uuid(), sa.ForeignKey("routes.id", ondelete="CASCADE"), nullable=False
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
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('invited', 'joined', 'declined', 'removed', 'left')", name="state"
        ),
        sa.UniqueConstraint("route_id", "user_id", name="uq_route_coauthors_route_user"),
    )
    op.create_index("ix_route_coauthors_route_id", "route_coauthors", ["route_id"])
    op.create_index("ix_route_coauthors_user_state", "route_coauthors", ["user_id", "state"])
    op.add_column(
        "routes", sa.Column("owner_consented_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint(
        "kind", "notifications", f"kind IN ({_KINDS_BEFORE}, {_COAUTHOR_KINDS})"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE kind IN ({_COAUTHOR_KINDS})")
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", f"kind IN ({_KINDS_BEFORE})")
    op.drop_column("routes", "owner_consented_at")
    op.drop_table("route_coauthors")
