"""Public transport lines, stops and timetables (BACKEND-12, spec 12b)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0074_transit"
down_revision: str | Sequence[str] | None = "0073_parking_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "transit_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("osm_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("operator", sa.String(length=255), nullable=True),
        sa.Column("network", sa.String(length=255), nullable=True),
        sa.Column("colour", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("suspend_reason", sa.Text(), nullable=True),
        sa.Column("needs_mapping", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("data_version", sa.String(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('bus', 'trolleybus', 'tram', 'train', 'share_taxi', 'ferry', 'cable_car')",
            name="ck_transit_lines_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'missing')", name="ck_transit_lines_status"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transit_lines"),
        sa.UniqueConstraint("osm_id", name="uq_transit_lines_osm_id"),
    )
    op.create_table(
        "transit_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("osm_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("to_name", sa.String(length=255), nullable=True),
        sa.Column("ptv2", sa.Boolean(), nullable=False),
        sa.Column("needs_mapping", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "shape",
            Geography(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("osm_interval", sa.String(length=64), nullable=True),
        sa.Column("osm_opening_hours", sa.String(length=255), nullable=True),
        sa.Column("present_in_osm", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["transit_lines.id"],
            name="fk_transit_variants_line_id_transit_lines",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transit_variants"),
        sa.UniqueConstraint("osm_id", name="uq_transit_variants_osm_id"),
    )
    op.create_index("ix_transit_variants_line_id", "transit_variants", ["line_id"])
    op.create_table(
        "transit_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("osm_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_transit_stops"),
        sa.UniqueConstraint("osm_id", name="uq_transit_stops_osm_id"),
    )
    op.execute("CREATE INDEX ix_transit_stops_location ON transit_stops USING gist (location)")
    op.create_table(
        "transit_variant_stops",
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("stop_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["transit_variants.id"],
            name="fk_transit_variant_stops_variant_id_transit_variants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stop_id"],
            ["transit_stops.id"],
            name="fk_transit_variant_stops_stop_id_transit_stops",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("variant_id", "seq", name="pk_transit_variant_stops"),
    )
    op.create_index("ix_transit_variant_stops_stop_id", "transit_variant_stops", ["stop_id"])
    op.create_table(
        "transit_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("days", sa.SmallInteger(), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("first_departure", sa.Time(), nullable=False),
        sa.Column("last_departure", sa.Time(), nullable=False),
        sa.Column("headway_minutes", sa.Integer(), nullable=True),
        sa.Column("departures", sa.ARRAY(sa.Time()), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.Date(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "(headway_minutes IS NOT NULL AND headway_minutes > 0) OR cardinality(departures) > 0",
            name="ck_transit_schedules_interval_or_departures",
        ),
        sa.CheckConstraint("days > 0 AND days < 128", name="ck_transit_schedules_days_mask"),
        sa.ForeignKeyConstraint(
            ["line_id"],
            ["transit_lines.id"],
            name="fk_transit_schedules_line_id_transit_lines",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transit_schedules"),
    )
    op.create_index("ix_transit_schedules_line_id", "transit_schedules", ["line_id"])


def downgrade() -> None:
    op.drop_table("transit_schedules")
    op.drop_table("transit_variant_stops")
    op.drop_table("transit_stops")
    op.drop_table("transit_variants")
    op.drop_table("transit_lines")
