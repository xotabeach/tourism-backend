"""Articles, blocks and comments (Workstream G, step 1)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_articles"
down_revision: str | Sequence[str] | None = "0047_runtime_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked'"
    ")"
)
_NEW_KINDS = (
    "kind IN ("
    "'route_review', 'route_published', 'route_rejected', "
    "'review_published', 'review_rejected', 'profile_like', "
    "'achievement_unlocked', 'support_reply', 'review_reply', "
    "'expert_granted', 'expert_revoked', "
    "'article_published', 'article_rejected', "
    "'article_comment', 'article_about_your_route'"
    ")"
)
_OLD_TARGET = (
    "target_type IS NULL OR target_type IN ('route', 'user', 'achievement', 'support_ticket')"
)
_NEW_TARGET = (
    "target_type IS NULL OR target_type IN "
    "('route', 'user', 'achievement', 'support_ticket', 'article')"
)
_OLD_ENTITY_TYPES = (
    "entity_type IN ('user', 'place', 'route', 'review', 'place_review', 'support_ticket')"
)
_NEW_ENTITY_TYPES = (
    "entity_type IN ('user', 'place', 'route', 'review', 'place_review', "
    "'support_ticket', 'article')"
)
_ARTICLE_KINDS = (
    "'article_published', 'article_rejected', 'article_comment', 'article_about_your_route'"
)


def upgrade() -> None:
    op.create_table(
        "articles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("related_route_id", sa.Uuid(), nullable=True),
        sa.Column("related_place_id", sa.Uuid(), nullable=True),
        sa.Column("cover_media_attachment_id", sa.Uuid(), nullable=True),
        sa.Column("moderator_note", sa.String(length=500), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        sa.CheckConstraint(
            "related_route_id IS NULL OR related_place_id IS NULL",
            name="single_subject",
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_route_id"], ["routes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_place_id"], ["places.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["cover_media_attachment_id"], ["media_attachments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_articles_author_user_id", "articles", ["author_user_id"])
    op.create_index("ix_articles_related_route_id", "articles", ["related_route_id"])
    op.create_index("ix_articles_related_place_id", "articles", ["related_place_id"])
    op.create_index("ix_articles_moderation_queue", "articles", ["status", "created_at"])
    op.create_index("ix_articles_author_status", "articles", ["author_user_id", "status"])
    op.create_index("ix_articles_published_feed", "articles", ["status", "published_at"])

    op.create_table(
        "article_blocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.String(length=16), nullable=False),
        sa.Column("text_content", sa.String(length=4000), nullable=True),
        sa.Column("media_attachment_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "block_type IN ('text', 'image')",
            name="block_type",
        ),
        # An image block's attachment stays nullable on purpose: the block
        # is created first and the file uploaded separately, so "reserved,
        # not yet uploaded" must be a legal row.
        sa.CheckConstraint(
            "(block_type = 'text' AND text_content IS NOT NULL "
            "AND media_attachment_id IS NULL) OR "
            "(block_type = 'image' AND text_content IS NULL)",
            name="block_shape",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["media_attachment_id"], ["media_attachments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id", "position", name="uq_article_blocks_article_position"),
    )
    op.create_index("ix_article_blocks_article_id", "article_blocks", ["article_id"])

    op.create_table(
        "article_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("reply_to_comment_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("moderator_note", sa.String(length=500), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reply_to_comment_id"], ["article_comments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_article_comments_article_id", "article_comments", ["article_id"])
    op.create_index("ix_article_comments_author_user_id", "article_comments", ["author_user_id"])
    op.create_index(
        "ix_article_comments_reply_to_comment_id", "article_comments", ["reply_to_comment_id"]
    )
    op.create_index(
        "ix_article_comments_feed", "article_comments", ["article_id", "status", "created_at"]
    )
    op.create_index(
        "ix_article_comments_moderation_queue", "article_comments", ["status", "created_at"]
    )

    # Articles carry images through the same media pipeline as reviews.
    op.drop_constraint(op.f("ck_media_attachments_entity_type"), "media_attachments", type_="check")
    op.create_check_constraint("entity_type", "media_attachments", _NEW_ENTITY_TYPES)

    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _NEW_TARGET)


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM notifications WHERE kind IN ({_ARTICLE_KINDS})"))
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _OLD_TARGET)

    # Article images must go before the constraint that no longer allows them.
    op.execute(sa.text("DELETE FROM media_attachments WHERE entity_type = 'article'"))
    op.drop_constraint(op.f("ck_media_attachments_entity_type"), "media_attachments", type_="check")
    op.create_check_constraint("entity_type", "media_attachments", _OLD_ENTITY_TYPES)

    op.drop_table("article_comments")
    op.drop_table("article_blocks")
    op.drop_table("articles")
