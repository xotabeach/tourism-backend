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
    # Taken at start so a later edit cannot raise the reward multiplier
    # (spec 14, R1). Snapshots before 0069 have none and read the route.
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    base_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Days the norms give; hand-set days never raise the points cap (D20).
    auto_day_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RoutingSnapshotDay(Base, UUIDPrimaryKeyMixin):
    """A day of the route as it was at start; immutable like its snapshot."""

    __tablename__ = "routing_snapshot_days"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "day_index", name="uq_routing_snapshot_days_day"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "route_routing_snapshots.id",
            ondelete="CASCADE",
            name="fk_routing_snapshot_days_snapshot",
        ),
        nullable=False,
        index=True,
    )
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    first_position: Mapped[int] = mapped_column(Integer, nullable=False)
    last_position: Mapped[int] = mapped_column(Integer, nullable=False)
    boundary_source: Mapped[str] = mapped_column(String(8), nullable=False)


class RoutingSnapshotSegment(Base, UUIDPrimaryKeyMixin):
    """A segment of a leg as it was at start; points and pace read these."""

    __tablename__ = "routing_snapshot_segments"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "leg_index", "seq", name="uq_routing_snapshot_segments_leg_seq"
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "route_routing_snapshots.id",
            ondelete="CASCADE",
            name="fk_routing_snapshot_segments_snapshot",
        ),
        nullable=False,
        index=True,
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_gain_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_loss_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RouteExecution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's attempt to walk a route."""

    __tablename__ = "route_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "points_status IN ('none', 'awarded', 'held', 'rejected')",
            name="points_status",
        ),
        CheckConstraint("computed_points >= 0", name="computed_points_non_negative"),
        Index("ix_route_executions_user_started", "user_id", "started_at"),
        Index(
            "uq_route_executions_one_active_per_user",
            "user_id",
            unique=True,
            # A paused run is still "the one you're on" — it must keep
            # blocking a second start the same way an active run does.
            postgresql_where=text("status IN ('active', 'paused')"),
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
    # When the run entered its current 'paused' state; None otherwise. Used
    # only to compute paused_duration_seconds on resume.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # «Закончить день» (spec 14a): a pause that ends a day of a multi-day run.
    # The day the walker is on is ``night_pauses + 1``.
    night_pauses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    night_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Cancelled before the last day, by the walker or for being idle: the
    # finished days are still paid (spec 14, D21).
    ended_early: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Total time spent paused across the whole run, so a completion summary
    # can report elapsed time net of pauses.
    paused_duration_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # Points actually granted on completion. Also the idempotency guard: a
    # replayed complete must not pay out twice.
    awarded_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # What the run earned after cooldown and daily-cap limits, whether or not
    # it was credited. ``awarded_points`` stays "credited to the balance" so
    # every existing reader keeps its meaning; a held run has computed > 0 and
    # awarded == 0. The daily cap sums this column.
    computed_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # none = not settled yet (or nothing earned), awarded = credited,
    # held = waiting for an operator, rejected = cancelled by an operator.
    points_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    # Why the run earned less than the route is worth: route_cooldown,
    # daily_cap or None.
    points_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


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
    # Rounded up so 500.1 metres cannot pass the 500-metre achievement rule.
    device_distance_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Expected leg from the previous stop, computed once when the run starts
    # (the routing snapshot is append-only and holds no per-leg data). NULL
    # for the first stop, for stops without coordinates and for runs that
    # started before anti-fraud shipped.
    leg_distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leg_estimate_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leg_estimate_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # What the pace check judges by (af_pace_source): the straight-line leg
    # while router legs are only observed (spec 12a, D4). NULL for runs that
    # started before, which fall back to leg_estimate_seconds.
    pace_estimate_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The mark came so soon after the previous one that it earns no stop points.
    mark_below_floor: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class RouteExecutionEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only ledger of execution mutations, including offline replays.

    The client event id makes a queued action safe to send twice: the second
    delivery finds its own row and returns the recorded outcome instead of
    mutating state again.  ``occurred_at`` keeps the untrusted device time next
    to the clamped ``effective_at`` that actually entered the run, so history
    stays auditable without trusting a phone clock.
    """

    __tablename__ = "route_execution_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('complete_stop', 'uncomplete_stop', 'complete', 'cancel', 'pause', "
            "'resume', 'end_day', 'finish_early')",
            name="action",
        ),
        UniqueConstraint(
            "user_id",
            "client_event_id",
            name="uq_route_execution_events_user_event",
        ),
        Index(
            "ix_route_execution_events_execution_recorded",
            "execution_id",
            "recorded_at",
        ),
    )

    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stop_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_execution_stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    client_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RoutePaceViolation(Base, UUIDPrimaryKeyMixin):
    """One suspicious stop mark. Retained 90 days, then purged.

    ``mode`` records whether it was observed in shadow or enforced, so a
    shadow period yields real "would have been flagged" data. ``counted`` is
    false for the follow-up marks of a batch, which score once.
    """

    __tablename__ = "route_pace_violations"
    __table_args__ = (
        CheckConstraint("kind IN ('too_fast', 'ahead')", name="kind"),
        CheckConstraint("mode IN ('shadow', 'enforce')", name="mode"),
        CheckConstraint("timing_source IN ('server', 'device')", name="timing_source"),
        CheckConstraint(
            "gps_verdict IS NULL OR gps_verdict IN ('at', 'behind', 'ahead', 'unknown')",
            name="gps_verdict",
        ),
        Index("ix_route_pace_violations_user_occurred", "user_id", "occurred_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stop_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_execution_stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    estimate_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gps_verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Distance to the marked stop rounded to 50 m. Raw coordinates are never
    # stored or logged.
    gps_distance_bucket_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timing_source: Mapped[str] = mapped_column(String(8), nullable=False)
    offline_sync: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    # When the mark really happened (effective time), which is what the
    # flag/block windows are measured on; ``created_at`` is when we saw it.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserFraudState(Base):
    """Per-user anti-fraud state; no row means "normal"."""

    __tablename__ = "user_fraud_state"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_flagged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ladder_level: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_offence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Violations older than this no longer count (set on a block or an admin reset).
    counters_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_trusted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoutePointsHold(Base, UUIDPrimaryKeyMixin):
    """Points of one run waiting for an operator (one row per execution)."""

    __tablename__ = "route_points_holds"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_route_points_holds_execution_id"),
        CheckConstraint("status IN ('held', 'approved', 'rejected')", name="status"),
        CheckConstraint("reason IN ('flag_retro', 'flag_forward')", name="reason"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("deducted_points >= 0", name="deducted_non_negative"),
        Index("ix_route_points_holds_status_created", "status", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # What the run is worth, and how much was actually taken back from an
    # already-credited balance (never more than the balance held).
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    deducted_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="held")
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_principals.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
