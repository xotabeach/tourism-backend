"""Article engagement fields, article_likes, and expanded block types (Workstream G, v2)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049_articles_v2"
down_revision: str | Sequence[str] | None = "0048_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_BLOCK_TYPES = "block_type IN ('text', 'image')"
_NEW_BLOCK_TYPES = "block_type IN ('text', 'image', 'quote', 'list', 'divider')"

_OLD_BLOCK_SHAPE = (
    "(block_type = 'text' AND text_content IS NOT NULL "
    "AND media_attachment_id IS NULL) OR "
    "(block_type = 'image' AND text_content IS NULL)"
)
_NEW_BLOCK_SHAPE = (
    "(block_type = 'text' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
    "AND caption IS NULL AND list_style IS NULL) OR "
    "(block_type = 'image' AND text_content IS NULL "
    "AND caption IS NULL AND list_style IS NULL) OR "
    "(block_type = 'quote' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
    "AND list_style IS NULL) OR "
    "(block_type = 'list' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
    "AND list_style IS NOT NULL AND caption IS NULL) OR "
    "(block_type = 'divider' AND text_content IS NULL AND media_attachment_id IS NULL "
    "AND caption IS NULL AND list_style IS NULL)"
)


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.add_column(
        "articles",
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "articles",
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "articles",
        sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column("article_blocks", sa.Column("caption", sa.String(length=80), nullable=True))
    op.add_column("article_blocks", sa.Column("list_style", sa.String(length=16), nullable=True))

    op.drop_constraint(op.f("ck_article_blocks_block_type"), "article_blocks", type_="check")
    op.create_check_constraint("block_type", "article_blocks", _NEW_BLOCK_TYPES)
    op.drop_constraint(op.f("ck_article_blocks_block_shape"), "article_blocks", type_="check")
    op.create_check_constraint("block_shape", "article_blocks", _NEW_BLOCK_SHAPE)

    op.create_table(
        "article_likes",
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("article_id", "user_id", name="pk_article_likes"),
    )
    op.create_index("ix_article_likes_user_id", "article_likes", ["user_id"])


def downgrade() -> None:
    op.drop_table("article_likes")

    op.execute(sa.text("DELETE FROM article_blocks WHERE block_type NOT IN ('text', 'image')"))
    op.drop_constraint(op.f("ck_article_blocks_block_shape"), "article_blocks", type_="check")
    op.create_check_constraint("block_shape", "article_blocks", _OLD_BLOCK_SHAPE)
    op.drop_constraint(op.f("ck_article_blocks_block_type"), "article_blocks", type_="check")
    op.create_check_constraint("block_type", "article_blocks", _OLD_BLOCK_TYPES)

    op.drop_column("article_blocks", "list_style")
    op.drop_column("article_blocks", "caption")

    op.drop_column("articles", "is_featured")
    op.drop_column("articles", "view_count")
    op.drop_column("articles", "like_count")
    op.drop_column("articles", "tags")
