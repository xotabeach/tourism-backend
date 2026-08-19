"""Persist reply context for route reviews."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_review_replies"
down_revision: str | Sequence[str] | None = "0022_review_media_expert_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_reviews",
        sa.Column("reply_to_review_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_route_reviews_reply_to_review_id_route_reviews",
        "route_reviews",
        "route_reviews",
        ["reply_to_review_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_route_reviews_reply_to_review_id",
        "route_reviews",
        ["reply_to_review_id"],
    )
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint(
        "kind",
        "notifications",
        "kind IN ("
        "'route_review', 'route_published', 'route_rejected', "
        "'review_published', 'review_rejected', 'profile_like', "
        "'achievement_unlocked', 'support_reply', 'review_reply'"
        ")",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM notifications WHERE kind = 'review_reply'"))
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint(
        "kind",
        "notifications",
        "kind IN ("
        "'route_review', 'route_published', 'route_rejected', "
        "'review_published', 'review_rejected', 'profile_like', "
        "'achievement_unlocked', 'support_reply'"
        ")",
    )
    op.drop_index("ix_route_reviews_reply_to_review_id", table_name="route_reviews")
    op.drop_constraint(
        "fk_route_reviews_reply_to_review_id_route_reviews",
        "route_reviews",
        type_="foreignkey",
    )
    op.drop_column("route_reviews", "reply_to_review_id")
