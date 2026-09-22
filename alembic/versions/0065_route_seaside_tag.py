"""Routes: the «Море» tag (BACKEND-19).

A route is a sea route when a stop is a beach or stands within 400 m of the
coastline (OSM coastline in route_terrain_features). Existing routes are
marked here once; later saves work it out from the stops, and an editor can
fix it in the admin.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_route_seaside_tag"
down_revision: str | Sequence[str] | None = "0064_profile_preferences_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied from routes/application/seaside.py: a migration must not change when
# the app code later does.
_SEASIDE_DISTANCE_METERS = 400


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("is_seaside", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        f"""
        UPDATE routes SET is_seaside = true
        WHERE id IN (
            SELECT rs.route_id
            FROM route_stops rs
            JOIN places p ON p.id = rs.place_id
            WHERE EXISTS (
                SELECT 1 FROM place_categories pc
                JOIN categories c ON c.id = pc.category_id
                WHERE pc.place_id = p.id AND c.slug = 'beach'
            )
            OR EXISTS (
                SELECT 1 FROM route_terrain_features f
                WHERE f.kind = 'coastline'
                  AND ST_DWithin(f.geometry, p.location, {_SEASIDE_DISTANCE_METERS})
            )
        )
        """
    )


def downgrade() -> None:
    op.drop_column("routes", "is_seaside")
