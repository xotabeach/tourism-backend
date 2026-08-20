"""Place planning facts, road events, and content enrichment provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_place_planning_enrichment"
down_revision: str | Sequence[str] | None = "0028_route_builder_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column(
            "typical_crowding",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("places", sa.Column("price_min_amount", sa.Integer(), nullable=True))
    op.add_column("places", sa.Column("price_max_amount", sa.Integer(), nullable=True))
    op.add_column(
        "places",
        sa.Column(
            "price_currency",
            sa.String(length=8),
            nullable=False,
            server_default="RUB",
        ),
    )
    op.add_column(
        "places",
        sa.Column("access_transport", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column("places", sa.Column("parking_available", sa.Boolean(), nullable=True))
    op.add_column(
        "places",
        sa.Column(
            "content_enrichment_status",
            sa.String(length=32),
            nullable=False,
            server_default="missing",
        ),
    )
    op.add_column(
        "places",
        sa.Column(
            "content_enrichment",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "places",
        sa.Column("proposed_slug", sa.String(length=150), nullable=True),
    )

    op.create_check_constraint(
        "ck_places_typical_crowding",
        "places",
        "typical_crowding IN ('unknown', 'low', 'medium', 'high')",
    )
    op.create_check_constraint(
        "ck_places_price_min_amount",
        "places",
        "price_min_amount IS NULL OR price_min_amount >= 0",
    )
    op.create_check_constraint(
        "ck_places_price_max_amount",
        "places",
        "price_max_amount IS NULL OR price_max_amount >= 0",
    )
    op.create_check_constraint(
        "ck_places_price_range_order",
        "places",
        "price_min_amount IS NULL OR price_max_amount IS NULL "
        "OR price_max_amount >= price_min_amount",
    )
    op.create_check_constraint(
        "ck_places_content_enrichment_status",
        "places",
        "content_enrichment_status IN "
        "('missing', 'generated_draft', 'editorial_reviewed', 'rejected')",
    )
    op.create_index("ix_places_typical_crowding", "places", ["typical_crowding"])
    op.create_index(
        "ix_places_content_enrichment_status",
        "places",
        ["content_enrichment_status"],
    )

    op.add_column(
        "routes",
        sa.Column(
            "typical_crowding",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("routes", sa.Column("price_min_amount", sa.Integer(), nullable=True))
    op.add_column("routes", sa.Column("price_max_amount", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_routes_typical_crowding",
        "routes",
        "typical_crowding IN ('unknown', 'low', 'medium', 'high')",
    )

    op.create_table(
        "road_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("region_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_name", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_external_id", sa.String(length=255), nullable=True),
        sa.Column(
            "affects_transport",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'scheduled', 'resolved')",
            name="ck_road_events_status",
        ),
        sa.CheckConstraint(
            "event_kind IN ('closure', 'restriction', 'congestion', 'other')",
            name="ck_road_events_kind",
        ),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_road_events_region_id", "road_events", ["region_id"])
    op.create_index("ix_road_events_status", "road_events", ["status"])
    op.create_index(
        "ix_road_events_source_external",
        "road_events",
        ["source_name", "source_external_id"],
        unique=True,
        postgresql_where=sa.text("source_name IS NOT NULL AND source_external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_road_events_source_external", table_name="road_events")
    op.drop_index("ix_road_events_status", table_name="road_events")
    op.drop_index("ix_road_events_region_id", table_name="road_events")
    op.drop_table("road_events")

    op.drop_constraint("ck_routes_typical_crowding", "routes", type_="check")
    op.drop_column("routes", "price_max_amount")
    op.drop_column("routes", "price_min_amount")
    op.drop_column("routes", "typical_crowding")

    op.drop_index("ix_places_content_enrichment_status", table_name="places")
    op.drop_index("ix_places_typical_crowding", table_name="places")
    op.drop_constraint("ck_places_content_enrichment_status", "places", type_="check")
    op.drop_constraint("ck_places_price_range_order", "places", type_="check")
    op.drop_constraint("ck_places_price_max_amount", "places", type_="check")
    op.drop_constraint("ck_places_price_min_amount", "places", type_="check")
    op.drop_constraint("ck_places_typical_crowding", "places", type_="check")
    op.drop_column("places", "proposed_slug")
    op.drop_column("places", "content_enrichment")
    op.drop_column("places", "content_enrichment_status")
    op.drop_column("places", "parking_available")
    op.drop_column("places", "access_transport")
    op.drop_column("places", "price_currency")
    op.drop_column("places", "price_max_amount")
    op.drop_column("places", "price_min_amount")
    op.drop_column("places", "typical_crowding")
