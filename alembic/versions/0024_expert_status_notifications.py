"""Notify users when expert status changes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_expert_status_notifications"
down_revision: str | Sequence[str] | None = "0023_review_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply'"
    ")"
)
_NEW_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked'"
    ")"
)


def upgrade() -> None:
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM notifications WHERE kind IN ('expert_granted', 'expert_revoked')")
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)
