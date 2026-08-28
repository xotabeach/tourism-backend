"""Public route execution API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RouteExecutionStatus = Literal["active", "completed", "cancelled"]


class RouteExecutionStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: UUID


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
    total_stops: int = Field(ge=0)
    completed_stops: int = Field(ge=0)
    required_stops: int = Field(ge=0)
    completed_required_stops: int = Field(ge=0)
    stops: list[RouteExecutionStopOut]
    created_at: datetime
    updated_at: datetime


class RouteExecutionListOut(BaseModel):
    items: list[RouteExecutionOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
