from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoutePublicationStatus = Literal[
    "draft",
    "pending_review",
    "published",
    "rejected",
    "deleted",
]


class UserRouteDraftIn(BaseModel):
    route_id: UUID | None = None
    name: str = Field(min_length=1, max_length=30)
    description: str = Field(default="", max_length=500)
    place_ids: list[UUID] = Field(min_length=2, max_length=22)
    filters: list[str] = Field(default_factory=list, max_length=20)
    pace: Literal["calm", "moderate", "active"] = "calm"
    difficulty: int = Field(default=3, ge=1, le=5)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Route name is required")
        return cleaned

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("place_ids")
    @classmethod
    def unique_places(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Route places must be unique")
        return value

    @field_validator("filters")
    @classmethod
    def clean_filters(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(len(item) > 80 for item in cleaned):
            raise ValueError("Filter is too long")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Route filters must be unique")
        return cleaned


class UserRouteDraftOut(BaseModel):
    id: UUID
    publication_status: RoutePublicationStatus
    updated_at: datetime


class UserRouteMediaOut(BaseModel):
    id: UUID
    public_path: str
    kind: Literal["image", "video"]
    position: int


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
    publication_status: str
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
