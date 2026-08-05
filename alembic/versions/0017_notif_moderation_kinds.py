"""Expand notification kinds for route/review moderation outcomes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_notif_moderation_kinds"
down_revision: str | Sequence[str] | None = "0016_device_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_KINDS = (
    "kind IN ("
    "'route_review', "
    "'route_published', "
    "'route_rejected', "
    "'review_published', "
    "'review_rejected'"
    ")"
)
_OLD_KINDS = "kind IN ('route_review')"


def upgrade() -> None:
    # Naming convention turns name "kind" into ck_notifications_kind.
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM notifications WHERE kind IN "
            "('route_published', 'route_rejected', 'review_published', 'review_rejected')"
        )
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)
