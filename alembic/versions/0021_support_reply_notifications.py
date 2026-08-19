"""Notify users when an operator replies to support."""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_support_reply_notifications"
down_revision: str | Sequence[str] | None = "0020_user_expert_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply'"
    ")"
)
_OLD_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked'"
    ")"
)
_NEW_TARGET = (
    "target_type IS NULL OR target_type IN ('route', 'user', 'achievement', 'support_ticket')"
)
_OLD_TARGET = "target_type IS NULL OR target_type IN ('route', 'user', 'achievement')"


def upgrade() -> None:
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _NEW_TARGET)


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE kind = 'support_reply'")
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _OLD_TARGET)
