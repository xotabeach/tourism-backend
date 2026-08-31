"""Article ORM models (Workstream G).

Its own module rather than a corner of ``routes``/``places``: an article
may point at a route, at a place, or at neither, so it does not belong to
either aggregate. Status vocabulary and moderation columns deliberately
mirror ``RouteReview`` so the moderation queue, admin views and
notification wiring stay one pattern instead of two.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ARTICLE_STATUSES = ("draft", "pending_review", "published", "rejected", "deleted")
# One status more than a review has: a review is written in one sitting,
# an article is not, so the author needs somewhere to leave it unfinished
# that moderation never sees.
COMMENT_STATUSES = ("pending_review", "published", "rejected", "deleted")
BLOCK_TYPES = ("text", "image")

MAX_TITLE_LENGTH = 120
MAX_TEXT_BLOCK_LENGTH = 4000
MAX_COMMENT_LENGTH = 2000
MAX_BLOCKS_PER_ARTICLE = 20
MAX_IMAGES_PER_ARTICLE = 12


class Article(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        # An article is about one thing on v1. Enforced here and not only in
        # the service so no future writer path can create a half-linked row.
        CheckConstraint(
            "related_route_id IS NULL OR related_place_id IS NULL",
            name="single_subject",
        ),
        Index("ix_articles_moderation_queue", "status", "created_at"),
        Index("ix_articles_author_status", "author_user_id", "status"),
        Index("ix_articles_published_feed", "status", "published_at"),
    )

    author_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(MAX_TITLE_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    related_route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    related_place_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Denormalized pointer to the first image block's attachment: the feed
    # renders a cover per card and must not walk every article's blocks.
    cover_media_attachment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="SET NULL"),
        nullable=True,
    )
    moderator_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Separate from moderated_at, which is also stamped on a rejection —
    # the feed sorts by when the article actually went live.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArticleBlock(Base, UUIDPrimaryKeyMixin):
    """One text or image block. No TimestampMixin: blocks are never edited
    individually, an article edit rebuilds the whole block list."""

    __tablename__ = "article_blocks"
    __table_args__ = (
        CheckConstraint("block_type IN ('text', 'image')", name="block_type"),
        # A block is exactly one of the two shapes, never a mix: a text
        # block always carries text and never an image, an image block
        # never carries text. An image block's attachment is deliberately
        # allowed to be NULL — the upload flow creates the block first so
        # the client can fix block order up front and retry only the file
        # upload on a bad connection, so "reserved, not yet uploaded" is a
        # legitimate state rather than the corruption this guards against.
        CheckConstraint(
            "(block_type = 'text' AND text_content IS NOT NULL "
            "AND media_attachment_id IS NULL) OR "
            "(block_type = 'image' AND text_content IS NULL)",
            name="block_shape",
        ),
        UniqueConstraint("article_id", "position", name="uq_article_blocks_article_position"),
    )

    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    block_type: Mapped[str] = mapped_column(String(16), nullable=False)
    text_content: Mapped[str | None] = mapped_column(String(MAX_TEXT_BLOCK_LENGTH), nullable=True)
    media_attachment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="CASCADE"),
        nullable=True,
    )


class ArticleComment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Discussion under an article — deliberately not a review: no rating,
    because this comments on the text rather than scoring a route."""

    __tablename__ = "article_comments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        Index("ix_article_comments_feed", "article_id", "status", "created_at"),
        Index("ix_article_comments_moderation_queue", "status", "created_at"),
    )

    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reply_to_comment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("article_comments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    body: Mapped[str] = mapped_column(String(MAX_COMMENT_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    moderator_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
