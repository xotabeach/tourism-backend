"""Separate MiniLM index for versioned public help, no tourist-index mutations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_support_help_embeddings"
down_revision = "0056_support_help"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_help_embeddings",
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_help_revisions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model_id", sa.String(128), primary_key=True),
        sa.Column("search_profile", sa.String(32), primary_key=True),
        sa.Column("passage_index", sa.Integer(), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Double), nullable=False),
        sa.CheckConstraint(
            "array_ndims(embedding) = 1 AND cardinality(embedding) = 384",
            name="embedding_dimensions",
        ),
    )


def downgrade() -> None:
    op.drop_table("support_help_embeddings")
