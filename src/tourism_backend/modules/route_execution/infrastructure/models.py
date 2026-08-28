"""Persisted route runs, routing snapshots and stop snapshots."""

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RouteRoutingSnapshot(Base, UUIDPrimaryKeyMixin):
    """Immutable routing facts used by a route run.

    A route can be edited or a provider can return different geometry later.
    Keeping the normalized result in its own append-only row means an active
    execution always refers to the exact route the user saw at start time.
    Application code deliberately never updates a snapshot; a changed
    fingerprint creates a new revision instead.
    """

    __tablename__ = "route_routing_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "route_id",
            "revision",
            name="uq_route_routing_snapshots_route_revision",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "quality_status IN "
            "('unknown', 'unverified', 'checking', 'verified', "
            "'verified_with_warnings', 'needs_review', 'unusable')",
            name="quality_status_allowed",
        ),
        CheckConstraint(
            "distance_meters IS NULL OR distance_meters >= 0",
            name="distance_non_negative",
        ),
        CheckConstraint(
            "movement_duration_seconds IS NULL OR movement_duration_seconds >= 0",
            name="movement_duration_non_negative",
        ),
        CheckConstraint(
            "total_duration_seconds IS NULL OR total_duration_seconds >= 0",
            name="total_duration_non_negative",
        ),
        CheckConstraint(
            "max_road_angle_degrees IS NULL OR "
            "(max_road_angle_degrees >= 0 AND max_road_angle_degrees <= 90)",
            name="road_angle_range",
        ),
        Index(
            "ix_route_routing_snapshots_route_captured",
            "route_id",
            "captured_at",
        ),
    )

    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry = mapped_column(
        Geography(geometry_type="LINESTRING", srid=4326),
        nullable=True,
    )
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    movement_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    visit_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buffer_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_gain_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_loss_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_altitude_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_altitude_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_road_angle_degrees: Mapped[float | None] = mapped_column(Float, nullable=True)
    road_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    quality_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    quality_policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    warnings: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    requested_filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    route_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RouteExecution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's attempt to walk a route."""

    __tablename__ = "route_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'cancelled')",
            name="status",
        ),
        Index("ix_route_executions_user_started", "user_id", "started_at"),
        Index(
            "uq_route_executions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    routing_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_routing_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_name: Mapped[str] = mapped_column(String(255), nullable=False)
    route_cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RouteExecutionStop(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stable stop snapshot belonging to one route execution."""

    __tablename__ = "route_execution_stops"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "position",
            name="uq_route_execution_stops_execution_position",
        ),
        CheckConstraint("position >= 1", name="position_positive"),
        Index("ix_route_execution_stops_execution_position", "execution_id", "position"),
    )

    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_stop_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    place_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    place_name: Mapped[str] = mapped_column(String(255), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
