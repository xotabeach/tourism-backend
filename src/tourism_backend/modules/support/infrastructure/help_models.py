"""Versioned public help, isolated from tourism knowledge and private tickets."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SupportHelpRevision(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "support_help_revisions"
    __table_args__ = (
        UniqueConstraint("article_id", "revision", "app_version", "language"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("status IN ('draft', 'published', 'withdrawn')", name="status_allowed"),
        CheckConstraint(
            "status != 'published' OR (approved_by IS NOT NULL AND "
            "published_at IS NOT NULL AND review_until IS NOT NULL)",
            name="publication_review_required",
        ),
        Index(
            "uq_support_help_current",
            "article_id",
            "app_version",
            "language",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        Index(
            "ix_support_help_fts",
            # Match PostgreSQL's reflected casts/parentheses so autogenerate
            # does not repeatedly drop and recreate this expression index.
            text(
                "to_tsvector('russian'::regconfig, "
                "(((title::text || ' '::text) || question::text) || ' '::text) || body)"
            ),
            postgresql_using="gin",
        ),
    )

    article_id: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    faq_id: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    question: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportHelpEmbedding(Base):
    """Small exact-search index; never shared with tourism or private tickets."""

    __tablename__ = "support_help_embeddings"
    __table_args__ = (
        CheckConstraint(
            "array_ndims(embedding) = 1 AND cardinality(embedding) = 384",
            name="embedding_dimensions",
        ),
    )

    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("support_help_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    search_profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    passage_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(ARRAY(Double), nullable=False)
