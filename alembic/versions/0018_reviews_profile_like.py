"""Allow multiple reviews per author; profile_like notification kind."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_reviews_profile_like"
down_revision: str | Sequence[str] | None = "0017_notif_moderation_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_KINDS = (
    "kind IN ("
    "'route_review', "
    "'route_published', "
    "'route_rejected', "
    "'review_published', "
    "'review_rejected', "
    "'profile_like'"
    ")"
)
_OLD_KINDS = (
    "kind IN ("
    "'route_review', "
    "'route_published', "
    "'route_rejected', "
    "'review_published', "
    "'review_rejected'"
    ")"
)
_NEW_TARGET = "target_type IS NULL OR target_type IN ('route', 'user')"
_OLD_TARGET = "target_type IS NULL OR target_type IN ('route')"


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_route_reviews_route_author"),
        "route_reviews",
        type_="unique",
    )
    op.create_index(
        "ix_route_reviews_route_author_status",
        "route_reviews",
        ["route_id", "author_user_id", "status"],
        unique=False,
    )

    # Naming convention turns short names into ck_notifications_*.
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)

    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _NEW_TARGET)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM notifications WHERE kind = 'profile_like'"))
    op.execute(
        sa.text(
            "UPDATE notifications SET target_type = NULL, target_id = NULL "
            "WHERE target_type = 'user'"
        )
    )

    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _OLD_TARGET)

    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)

    op.drop_index("ix_route_reviews_route_author_status", table_name="route_reviews")
    op.create_unique_constraint(
        op.f("uq_route_reviews_route_author"),
        "route_reviews",
        ["route_id", "author_user_id"],
    )
