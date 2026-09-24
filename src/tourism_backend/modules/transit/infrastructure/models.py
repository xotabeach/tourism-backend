"""Public transport of Crimea: lines, their variants and stops from OSM,
and the timetables editors enter by hand (spec 12b)."""

from datetime import date, time
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

LINE_KINDS = ("bus", "trolleybus", "tram", "train", "share_taxi", "ferry", "cable_car")


class TransitLine(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A line as passengers know it («троллейбус 51»), grouping its variants.

    Fields from OSM are refreshed by every import; ``status`` and the
    suspension reason are the editors' and survive it (spec 12b, D7).
    """

    __tablename__ = "transit_lines"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('bus', 'trolleybus', 'tram', 'train', 'share_taxi', 'ferry', 'cable_car')",
            name="kind",
        ),
        CheckConstraint("status IN ('active', 'suspended', 'missing')", name="status"),
        CheckConstraint("speed_kmh IS NULL OR speed_kmh BETWEEN 3 AND 150", name="speed"),
    )

    # «m<relation>» for an OSM route_master, «v<relation>» for a lone route,
    # «manual-…» for a line an editor added (a cable car, say).
    osm_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network: Mapped[str | None] = mapped_column(String(255), nullable=True)
    colour: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # active: planned on when it has a timetable; suspended by an editor;
    # missing: gone from OSM at the last import (kept, never deleted).
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    suspend_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # No variant has a usable stop order: not planned on until fixed (D8).
    needs_mapping: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    data_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Average speed for the time between stops; empty means the kind's
    # default (spec 12b, section 2). An editor sets it for a slow line.
    speed_kmh: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class TransitVariant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One direction of a line: an OSM route relation with its stops in order."""

    __tablename__ = "transit_variants"

    line_id: Mapped[UUID] = mapped_column(
        ForeignKey("transit_lines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    osm_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ptv2: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_mapping: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    shape = mapped_column(
        Geography(geometry_type="LINESTRING", srid=4326, spatial_index=False), nullable=True
    )
    # What OSM says, as a hint for the editor filling the timetable in.
    osm_interval: Mapped[str | None] = mapped_column(String(64), nullable=True)
    osm_opening_hours: Mapped[str | None] = mapped_column(String(255), nullable=True)
    present_in_osm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class TransitStop(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transit_stops"

    osm_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )


class TransitVariantStop(Base):
    __tablename__ = "transit_variant_stops"

    variant_id: Mapped[UUID] = mapped_column(
        ForeignKey("transit_variants.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    stop_id: Mapped[UUID] = mapped_column(
        ForeignKey("transit_stops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TransitSchedule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A period of a line's service, by interval or by exact departures.

    Entered by editors with its source and check date (D7, D8); a line is
    planned on by time only when it has one (D24).
    """

    __tablename__ = "transit_schedules"
    __table_args__ = (
        CheckConstraint(
            "(headway_minutes IS NOT NULL AND headway_minutes > 0) OR cardinality(departures) > 0",
            name="interval_or_departures",
        ),
        CheckConstraint("days > 0 AND days < 128", name="days_mask"),
    )

    line_id: Mapped[UUID] = mapped_column(
        ForeignKey("transit_lines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    # Monday = 1, Tuesday = 2, … Sunday = 64.
    days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_departure: Mapped[time] = mapped_column(Time, nullable=False)
    last_departure: Mapped[time] = mapped_column(Time, nullable=False)
    headway_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departures: Mapped[list[time] | None] = mapped_column(ARRAY(Time), nullable=True)
    # A link, or «звонок перевозчику 2026-09-24» (D7); required.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    checked_at: Mapped[date] = mapped_column(Date, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
