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
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ARTICLE_STATUSES = ("draft", "pending_review", "published", "rejected", "deleted")
# One status more than a review has: a review is written in one sitting,
# an article is not, so the author needs somewhere to leave it unfinished
# that moderation never sees.
COMMENT_STATUSES = ("pending_review", "published", "rejected", "deleted")
BLOCK_TYPES = ("text", "image", "quote", "list", "divider")
LIST_STYLES = ("bullet", "numbered")

MAX_TITLE_LENGTH = 120
MAX_TEXT_BLOCK_LENGTH = 4000
MAX_COMMENT_LENGTH = 2000
MAX_BLOCKS_PER_ARTICLE = 20
MAX_IMAGES_PER_ARTICLE = 12
MAX_TAGS_PER_ARTICLE = 5
MAX_LIST_ITEMS = 15
MAX_LIST_ITEM_LENGTH = 200
MAX_QUOTE_CAPTION_LENGTH = 80


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
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # Denormalized on purpose (unlike review/favorite counts elsewhere, which
    # are always aggregated live): a like is a per-request hot path on the
    # reading screen, and an article's like count doesn't need to reflect a
    # moderation state the way review aggregates do.
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ArticleBlock(Base, UUIDPrimaryKeyMixin):
    """One text or image block. No TimestampMixin: blocks are never edited
    individually, an article edit rebuilds the whole block list."""

    __tablename__ = "article_blocks"
    __table_args__ = (
        CheckConstraint(
            "block_type IN ('text', 'image', 'quote', 'list', 'divider')", name="block_type"
        ),
        # A block is exactly one of five shapes, never a mix: text/quote/list
        # carry text (list also requires list_style), image/divider carry
        # neither. An image block's attachment is deliberately allowed to be
        # NULL — the upload flow creates the block first so the client can
        # fix block order up front and retry only the file upload on a bad
        # connection, so "reserved, not yet uploaded" is a legitimate state
        # rather than the corruption this guards against. `caption` is quote-only,
        # `list_style` is list-only.
        CheckConstraint(
            "(block_type = 'text' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
            "AND caption IS NULL AND list_style IS NULL) OR "
            "(block_type = 'image' AND text_content IS NULL "
            "AND caption IS NULL AND list_style IS NULL) OR "
            "(block_type = 'quote' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
            "AND list_style IS NULL) OR "
            "(block_type = 'list' AND text_content IS NOT NULL AND media_attachment_id IS NULL "
            "AND list_style IS NOT NULL AND caption IS NULL) OR "
            "(block_type = 'divider' AND text_content IS NULL AND media_attachment_id IS NULL "
            "AND caption IS NULL AND list_style IS NULL)",
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
    caption: Mapped[str | None] = mapped_column(String(MAX_QUOTE_CAPTION_LENGTH), nullable=True)
    list_style: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ArticleLike(Base):
    """A like is a plain toggle, not a moderated entity — no TimestampMixin,
    just the one timestamp that exists (mirrors `FavoritePlace`/`FavoriteRoute`)."""

    __tablename__ = "article_likes"
    __table_args__ = (PrimaryKeyConstraint("article_id", "user_id", name="pk_article_likes"),)

    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArticleBookmark(Base):
    """Saved-for-later article — same shape as `ArticleLike`, different
    meaning: a like is public appreciation, a bookmark is a private reading
    list. Kept in `content` rather than `favorites` so every article-shaped
    relation lives in one module."""

    __tablename__ = "article_bookmarks"
    __table_args__ = (PrimaryKeyConstraint("article_id", "user_id", name="pk_article_bookmarks"),)

    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
