"""Typed columns for OSM tags that were only living in source_payload.

ADR-009 P0.3: `description`, `ele`, `opening_hours`, `website`, `phone` and
`surface` are downloaded for a meaningful share of places but were kept only
as raw JSONB. These three had no column to be promoted into.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_place_osm_promoted_fields"
down_revision: str | Sequence[str] | None = "0032_knowledge_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("places", sa.Column("elevation_meters", sa.Integer(), nullable=True))
    # Raw OSM `opening_hours` expression. Untrusted free text: parsed at read
    # time, never treated as an authoritative closure source on its own.
    op.add_column("places", sa.Column("opening_hours_raw", sa.Text(), nullable=True))
    op.add_column("places", sa.Column("surface", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        "ck_places_elevation_meters",
        "places",
        "elevation_meters IS NULL OR (elevation_meters BETWEEN -500 AND 9000)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_places_elevation_meters", "places", type_="check")
    op.drop_column("places", "surface")
    op.drop_column("places", "opening_hours_raw")
    op.drop_column("places", "elevation_meters")
