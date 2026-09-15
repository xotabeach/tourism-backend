"""Add aliases and source provenance for non-city locality search."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058_locality_search_metadata"
down_revision: str | Sequence[str] | None = "0057_support_help_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("localities", sa.Column("aliases", postgresql.ARRAY(sa.String(255))))
    op.add_column("localities", sa.Column("population", sa.Integer()))
    op.add_column("localities", sa.Column("source_external_id", sa.String(255)))
    op.add_column("localities", sa.Column("source_license", sa.String(128)))
    op.create_index(
        "uq_localities_source_external_id",
        "localities",
        ["source_name", "source_external_id"],
        unique=True,
        postgresql_where=sa.text("source_name IS NOT NULL AND source_external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_localities_source_external_id", table_name="localities")
    op.drop_column("localities", "source_license")
    op.drop_column("localities", "source_external_id")
    op.drop_column("localities", "population")
    op.drop_column("localities", "aliases")
