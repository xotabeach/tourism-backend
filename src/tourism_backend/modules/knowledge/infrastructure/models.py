"""knowledge_chunks: RAG long-form storage (narrative only).

Design notes
------------
* ``embedding vector(384)`` is created by migration 0032 (raw SQL / pgvector).
  It is intentionally NOT mapped on the ORM so the mapper stays importable
  without the optional ``pgvector`` pip package. Reads/writes go through
  ``TourismKnowledgeRetriever`` / ingest via parameterized ``CAST(:vec AS vector)``.
* Chunks are *narrative* (history, tips, how-to). Hard facts stay in PostGIS.
* See ADR-008.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One retrievable narrative chunk (a semantic section, not a window)."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_seq", name="uq_knowledge_chunks_doc_seq"),
        Index("ix_knowledge_chunks_place_id", "place_id"),
        Index("ix_knowledge_chunks_content_type", "content_type"),
        Index("ix_knowledge_chunks_region", "region"),
    )

    doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)

    place_id: Mapped[UUID | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="crimea")
    locality: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default="overview")

    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
