"""Public route execution API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tourism_backend.modules.routes.application.schemas import (
    RouteGeometryOut,
    RouteQualityStatus,
)

RouteExecutionStatus = Literal["active", "completed", "cancelled"]
RouteExecutionEventAction = Literal["complete_stop", "complete", "cancel"]


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
    sync: RouteExecutionSyncOut | None = None
    created_at: datetime
    updated_at: datetime


class RouteExecutionListOut(BaseModel):
    items: list[RouteExecutionOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
