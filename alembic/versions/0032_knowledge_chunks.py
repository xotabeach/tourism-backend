"""knowledge_chunks table + pgvector for RAG narrative storage.

Requires the ``vector`` extension (pgvector) to be installed in the Postgres
instance. The image must include it (e.g. build on pgvector/pgvector, or
``apt install postgresql-16-pgvector``). The extension is created idempotently.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_knowledge_chunks"
down_revision: str | Sequence[str] | None = "0031_session_confirmed_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 384


def upgrade() -> None:
    # Idempotent: no-op if already present.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("chunk_seq", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("license", sa.String(64), nullable=True),
        sa.Column("place_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("region", sa.String(64), nullable=False, server_default="crimea"),
        sa.Column("locality", sa.String(128), nullable=True),
        sa.Column("lang", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("content_type", sa.String(32), nullable=False, server_default="overview"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # Actual vector column via raw DDL (can't express in sa.Column portably).
        sa.Column("embedding_dim_placeholder", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("doc_id", "chunk_seq", name="uq_knowledge_chunks_doc_seq"),
    )
    # Drop the placeholder column and add the real vector column + index.
    op.drop_column("knowledge_chunks", "embedding_dim_placeholder")
    op.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN embedding vector({_EMBEDDING_DIM})")
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_model", sa.String(64), nullable=True),
    )
    # HNSW index for fast approximate nearest-neighbor on 384-dim vectors.
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.create_index("ix_knowledge_chunks_place_id", "knowledge_chunks", ["place_id"])
    op.create_index("ix_knowledge_chunks_content_type", "knowledge_chunks", ["content_type"])
    op.create_index("ix_knowledge_chunks_region", "knowledge_chunks", ["region"])


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    # Leave the extension in place; dropping it is destructive and optional.
