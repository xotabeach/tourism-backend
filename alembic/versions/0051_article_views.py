"""Unique article views — one per reader, so the counter means something.

`view_count` used to be bumped on every open, so a reload or a habit of
re-checking one's own article inflated it without limit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_article_views"
down_revision: str | Sequence[str] | None = "0050_article_bookmarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_views",
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "user_id", name="pk_article_views"),
    )
    op.create_index("ix_article_views_user_id", "article_views", ["user_id"])


def downgrade() -> None:
    op.drop_table("article_views")
