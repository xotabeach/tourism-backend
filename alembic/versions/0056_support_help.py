"""Isolated, immutable-content help revisions and Russian full-text search."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_support_help"
down_revision: str | Sequence[str] | None = "0055_proposal_preview"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_help_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("article_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("faq_id", sa.String(80), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("question", sa.String(180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(100)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("review_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("article_id", "revision", "app_version", "language"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("status IN ('draft', 'published', 'withdrawn')", name="status_allowed"),
        sa.CheckConstraint(
            "status != 'published' OR (approved_by IS NOT NULL AND "
            "published_at IS NOT NULL AND review_until IS NOT NULL)",
            name="publication_review_required",
        ),
    )
    op.create_index(
        "uq_support_help_current",
        "support_help_revisions",
        ["article_id", "app_version", "language"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.execute(
        "CREATE INDEX ix_support_help_fts ON support_help_revisions USING gin "
        "(to_tsvector('russian', title || ' ' || question || ' ' || body))"
    )


def downgrade() -> None:
    op.drop_table("support_help_revisions")
