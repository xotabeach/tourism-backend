from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RouteStopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    place_id: UUID
    place_name: str
    place_slug: str
    visit_duration_minutes: int | None
    note: str | None
    is_optional: bool
    lng: float | None = None
    lat: float | None = None


class RouteListItemOut(BaseModel):
    id: UUID
    region_id: UUID
    name: str
    slug: str
    short_description: str | None
    source: str
    visibility: str
    lifecycle_status: str
    estimated_duration_minutes: int | None
    distance_meters: int | None
    difficulty: str | None
    transport_mode: str | None
    is_round_trip: bool
    suitable_for_children: bool | None
    pets_allowed: bool | None
    seasonality: list[str] | None
    stops_count: int
    author_label: str | None
    cover_image_url: str | None = None
    owner_user_id: UUID | None = None
    author_avatar_url: str | None = None


class RouteDetailOut(RouteListItemOut):
    description: str | None
    budget_notes: str | None
    accessibility: dict[str, object] | None
    freshness_status: str
    stops: list[RouteStopOut] = Field(default_factory=list)


class RouteListOut(BaseModel):
    items: list[RouteListItemOut]
    total: int
    limit: int
    offset: int
