"""Public route execution API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tourism_backend.modules.routes.application.schemas import (
    RouteGeometryOut,
    RouteQualityStatus,
)

RouteExecutionStatus = Literal["active", "paused", "completed", "cancelled"]
RouteExecutionEventAction = Literal[
    "complete_stop",
    "uncomplete_stop",
    "complete",
    "cancel",
    "pause",
    "resume",
    "end_day",
    "finish_early",
]
PointsStatus = Literal["none", "awarded", "held", "rejected"]
PaceVerdictOut = Literal["ok", "too_fast", "ahead", "unknown", "skipped"]


class RouteExecutionStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: UUID


class RouteExecutionEventIn(BaseModel):
    """Optional idempotency envelope for a mutation that may be replayed.

    Both fields stay optional so an online client can keep calling the
    endpoints without a body; an offline outbox sends them to make a retry
    safe and to report when the action really happened.
    """

    model_config = ConfigDict(extra="forbid")

    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


class PositionIn(BaseModel):
    """Optional position sent with a stop mark. Judged once, never stored."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)


class RouteExecutionStopMarkIn(RouteExecutionEventIn):
    """Stop mark: the idempotency envelope plus an optional position."""

    position: PositionIn | None = None


class RouteExecutionSyncOut(BaseModel):
    """What the server did with a (possibly replayed) mutation."""

    action: RouteExecutionEventAction
    client_event_id: UUID | None
    occurred_at: datetime | None
    effective_at: datetime
    recorded_at: datetime
    replayed: bool = False
    applied: bool = True


class RouteExecutionRoutingOut(BaseModel):
    """The immutable route facts captured when execution started."""

    snapshot_id: UUID
    revision: int = Field(ge=1)
    captured_at: datetime
    route_updated_at: datetime | None
    provider: str | None
    provider_version: str | None
    transport_mode: str | None
    geometry: RouteGeometryOut | None
    distance_meters: int | None = Field(default=None, ge=0)
    movement_duration_seconds: int | None = Field(default=None, ge=0)
    visit_duration_minutes: int | None = Field(default=None, ge=0)
    transfer_duration_seconds: int | None = Field(default=None, ge=0)
    buffer_duration_seconds: int | None = Field(default=None, ge=0)
    total_duration_seconds: int | None = Field(default=None, ge=0)
    elevation_gain_meters: int | None = Field(default=None, ge=0)
    elevation_loss_meters: int | None = Field(default=None, ge=0)
    min_altitude_meters: int | None
    max_altitude_meters: int | None
    max_road_angle_degrees: float | None = Field(default=None, ge=0, le=90)
    road_types: list[str] = Field(default_factory=list, max_length=32)
    quality_status: RouteQualityStatus
    quality_policy_version: str | None
    warnings: list[str] = Field(default_factory=list, max_length=32)


class RouteExecutionStopOut(BaseModel):
    id: UUID
    route_stop_id: UUID | None
    place_id: UUID | None
    position: int = Field(ge=1)
    place_name: str
    lat: float | None
    lng: float | None
    is_optional: bool
    completed_at: datetime | None
    # Expected leg from the previous stop; None for the first stop, for stops
    # without coordinates and for runs that started before anti-fraud shipped.
    leg_distance_meters: int | None = Field(default=None, ge=0)
    leg_estimate_seconds: int | None = Field(default=None, ge=0)
    leg_estimate_source: Literal["provider", "straight_line"] | None = None
    # Client hint threshold: warn before sending a mark faster than this. Only
    # present while anti-fraud is enforcing; None means "never warn".
    pace_warn_below_seconds: int | None = Field(default=None, ge=0)


class AntiFraudOut(BaseModel):
    """Present only while enforcing; tells the client hints are meaningful."""

    mode: Literal["enforce"] = "enforce"
    gps_tolerance_m: int = Field(ge=0)
    gps_min_accuracy_m: int = Field(ge=0)
    # The limits a client quotes when it explains why points were withheld.
    route_cooldown_days: int = Field(default=0, ge=0)
    daily_points_cap: int = Field(default=0, ge=0)


class RouteExecutionOut(BaseModel):
    id: UUID
    route_id: UUID | None
    route_name: str
    route_cover_url: str | None
    status: RouteExecutionStatus
    started_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    routing: RouteExecutionRoutingOut | None = None
    total_stops: int = Field(ge=0)
    completed_stops: int = Field(ge=0)
    required_stops: int = Field(ge=0)
    completed_required_stops: int = Field(ge=0)
    stops: list[RouteExecutionStopOut]
    # Travel points granted for finishing this route (0 while it is active).
    awarded_points: int = Field(default=0, ge=0)
    # none = not settled, awarded = credited, held = waiting for an operator,
    # rejected = cancelled by an operator. ``points_reason`` says why a run
    # earned less than the route is worth (route_cooldown, daily_cap).
    points_status: PointsStatus = "none"
    points_reason: str | None = None
    held_points: int = Field(default=0, ge=0)
    antifraud: AntiFraudOut | None = None
    # Server's read of the mark that produced this response; None otherwise.
    pace_verdict: PaceVerdictOut | None = None
    # Total time spent paused so far — lets a client report elapsed time net
    # of pauses without re-deriving it from the event ledger.
    paused_duration_seconds: int = Field(default=0, ge=0)
    # Start of the current pause, so a paused run's timer can stand still
    # (the pause only enters paused_duration_seconds on resume). FRONTEND-34.
    paused_at: datetime | None = None
    # Latest of start, a stop mark and a resume: what «давно не отмечали
    # точки» is measured from. FRONTEND-34.
    last_activity_at: datetime | None = None
    # A finished run whose route this person already reviewed: the home card
    # does not ask for a review then. FRONTEND-34.
    my_review_exists: bool = False
    # Multi-day runs (spec 14a): «День current_day из planned_days». The
    # walker may take longer than planned: current_day can exceed it.
    planned_days: int = Field(default=1, ge=1)
    current_day: int = Field(default=1, ge=1)
    # Resting after «Закончить день»; resume starts the next day.
    night_paused: bool = False
    # Cancelled before the last day; the finished days were paid (D21).
    ended_early: bool = False
    sync: RouteExecutionSyncOut | None = None
    created_at: datetime
    updated_at: datetime


class RouteExecutionListOut(BaseModel):
    items: list[RouteExecutionOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
