"""Editorial routes and route stops schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0003_editorial_routes"
down_revision: str | Sequence[str] | None = "0002_geography_and_places"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("region_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=150), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("distance_meters", sa.Integer(), nullable=True),
        sa.Column("difficulty", sa.String(length=32), nullable=True),
        sa.Column("budget_notes", sa.Text(), nullable=True),
        sa.Column("seasonality", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("transport_mode", sa.String(length=32), nullable=True),
        sa.Column(
            "is_round_trip",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("suitable_for_children", sa.Boolean(), nullable=True),
        sa.Column("pets_allowed", sa.Boolean(), nullable=True),
        sa.Column("accessibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "geometry",
            Geography(geometry_type="LINESTRING", srid=4326),
            nullable=True,
        ),
        sa.Column("author_label", sa.String(length=255), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("region_id", "slug", name="uq_routes_region_slug"),
        sa.CheckConstraint(
            "source IN ('editorial', 'generated', 'user_created')",
            name="ck_routes_source",
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'unlisted', 'public')",
            name="ck_routes_visibility",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('draft', 'active', 'archived')",
            name="ck_routes_lifecycle_status",
        ),
    )
    op.create_index("ix_routes_region_id", "routes", ["region_id"])
    op.create_index("ix_routes_owner_user_id", "routes", ["owner_user_id"])
    op.create_index("ix_routes_source", "routes", ["source"])
    op.create_index(
        "ix_routes_public_catalog",
        "routes",
        ["source", "visibility", "lifecycle_status", "region_id"],
    )
    op.execute("CREATE INDEX ix_routes_geometry ON routes USING GIST (geometry)")

    op.create_table(
        "route_stops",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("place_entrance_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("visit_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "is_optional",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["place_entrance_id"],
            ["place_entrances.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("route_id", "position", name="uq_route_stops_route_position"),
        sa.CheckConstraint("position >= 1", name="ck_route_stops_position_positive"),
    )
    op.create_index("ix_route_stops_route_id", "route_stops", ["route_id"])
    op.create_index("ix_route_stops_place_id", "route_stops", ["place_id"])


def downgrade() -> None:
    op.drop_table("route_stops")
    op.execute("DROP INDEX IF EXISTS ix_routes_geometry")
    op.drop_index("ix_routes_public_catalog", table_name="routes")
    op.drop_index("ix_routes_source", table_name="routes")
    op.drop_index("ix_routes_owner_user_id", table_name="routes")
    op.drop_index("ix_routes_region_id", table_name="routes")
    op.drop_table("routes")
