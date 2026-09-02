"""Saved articles — the reading list behind the favorites screen (Workstream G, v2)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_article_bookmarks"
down_revision: str | Sequence[str] | None = "0049_articles_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_bookmarks",
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "user_id", name="pk_article_bookmarks"),
    )
    op.create_index("ix_article_bookmarks_user_id", "article_bookmarks", ["user_id"])


def downgrade() -> None:
    op.drop_table("article_bookmarks")
