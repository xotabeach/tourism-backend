"""Public car parks from OSM for the walk from the car (BACKEND-26, spec 14b)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0070_parkings"
down_revision: str | Sequence[str] | None = "0069_route_days_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parkings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("osm_id", sa.String(length=32), nullable=False),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("access", sa.String(length=32), nullable=True),
        sa.Column("fee", sa.String(length=32), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("data_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_parkings"),
        sa.UniqueConstraint("osm_id", name="uq_parkings_osm_id"),
    )
    op.execute("CREATE INDEX ix_parkings_location ON parkings USING gist (location)")


def downgrade() -> None:
    op.drop_table("parkings")
