"""Independent OSM coastline/trail geometry for the segment-level terrain gate."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0044_route_terrain_features"
down_revision: str | Sequence[str] | None = "0043_route_execution_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_terrain_features",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "geometry",
            Geography(geometry_type="LINESTRING", srid=4326),
            nullable=False,
        ),
        sa.Column("source_osm_id", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('coastline', 'trail')",
            name="ck_route_terrain_features_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_terrain_features_kind",
        "route_terrain_features",
        ["kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_terrain_features_kind",
        table_name="route_terrain_features",
    )
    op.drop_table("route_terrain_features")
