"""Add place import provenance and route-planning facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_place_import_foundation"
down_revision: str | Sequence[str] | None = "0024_expert_status_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("places", sa.Column("source_external_id", sa.String(255), nullable=True))
    op.add_column("places", sa.Column("source_license", sa.String(128), nullable=True))
    op.add_column(
        "places",
        sa.Column(
            "source_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "places",
        sa.Column(
            "payment_status",
            sa.String(16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("places", sa.Column("recommended_visit_minutes", sa.Integer(), nullable=True))
    op.add_column("places", sa.Column("is_suitable_for_pets", sa.Boolean(), nullable=True))
    op.add_column(
        "places",
        sa.Column(
            "data_quality_status",
            sa.String(32),
            nullable=False,
            server_default="needs_review",
        ),
    )

    op.execute("UPDATE places SET payment_status = CASE WHEN is_paid THEN 'paid' ELSE 'free' END")
    op.execute(
        "UPDATE places SET data_quality_status = 'editorial_reviewed' WHERE source_name = 'seed'"
    )

    op.create_check_constraint(
        "ck_places_payment_status",
        "places",
        "payment_status IN ('unknown', 'free', 'paid')",
    )
    op.create_check_constraint(
        "ck_places_recommended_visit_minutes",
        "places",
        "recommended_visit_minutes IS NULL OR recommended_visit_minutes BETWEEN 1 AND 1440",
    )
    op.create_check_constraint(
        "ck_places_data_quality_status",
        "places",
        "data_quality_status IN "
        "('needs_review', 'auto_validated', 'editorial_reviewed', 'rejected')",
    )
    op.create_index("ix_places_payment_status", "places", ["payment_status"])
    op.create_index("ix_places_difficulty", "places", ["difficulty"])
    op.create_index(
        "uq_places_source_external_id",
        "places",
        ["source_name", "source_external_id"],
        unique=True,
        postgresql_where=sa.text("source_name IS NOT NULL AND source_external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_places_source_external_id", table_name="places")
    op.drop_index("ix_places_difficulty", table_name="places")
    op.drop_index("ix_places_payment_status", table_name="places")
    op.drop_constraint("ck_places_data_quality_status", "places", type_="check")
    op.drop_constraint("ck_places_recommended_visit_minutes", "places", type_="check")
    op.drop_constraint("ck_places_payment_status", "places", type_="check")
    op.drop_column("places", "data_quality_status")
    op.drop_column("places", "is_suitable_for_pets")
    op.drop_column("places", "recommended_visit_minutes")
    op.drop_column("places", "payment_status")
    op.drop_column("places", "source_payload")
    op.drop_column("places", "source_license")
    op.drop_column("places", "source_external_id")
