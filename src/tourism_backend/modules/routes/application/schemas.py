from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoutePublicationStatus = Literal[
    "draft",
    "pending_review",
    "published",
    "rejected",
    "deleted",
]
RouteCatalogSort = Literal[
    "default",
    "popular",
    "recent",
    "name_asc",
    "name_desc",
    "date_newest",
    "date_oldest",
]
RouteSource = Literal["editorial", "generated", "user_created"]
RouteQualityStatus = Literal[
    "unknown",
    "unverified",
    "checking",
    "verified",
    "verified_with_warnings",
    "needs_review",
    "unusable",
]


class UserRouteDraftIn(BaseModel):
    route_id: UUID | None = None
    # Generated on the device when the local draft is created. A save without a
    # route_id that repeats a key already used by this author updates that draft
    # instead of creating another (a lost response must not cause duplicates).
    client_draft_id: str | None = Field(default=None, min_length=8, max_length=36)
    # The server's updated_at the author last saw. If the draft was changed
    # since (another device), the save is refused with 409 draft_conflict.
    expected_updated_at: datetime | None = None
    name: str = Field(min_length=1, max_length=30)
    description: str = Field(default="", max_length=500)
    place_ids: list[UUID] = Field(min_length=2, max_length=22)
    filters: list[str] = Field(default_factory=list, max_length=20)
    pace: Literal["calm", "moderate", "active"] = "calm"
    difficulty: int = Field(default=3, ge=1, le=5)
    # Spec 17: True keeps ``difficulty`` as the author's rating, False is
    # «Авто». Older apps leave it out and always send a number (default 3):
    # their number changes a rating only when the author changed it (D15).
    difficulty_manual: bool | None = None
    # Places after which the author ends a day (spec 14a). Absent: keep the
    # days as they are (older apps); empty: split by the norms again.
    day_breaks: list[UUID] | None = Field(default=None, max_length=21)

    @model_validator(mode="after")
    def breaks_follow_the_stops(self) -> "UserRouteDraftIn":
        if not self.day_breaks:
            return self
        order = {place_id: index for index, place_id in enumerate(self.place_ids)}
        positions = [order.get(place_id) for place_id in self.day_breaks]
        if (
            any(position is None for position in positions)
            or positions != sorted(set(positions))  # type: ignore[type-var]
            or positions[-1] == len(self.place_ids) - 1
        ):
            raise ValueError("day_breaks must be route places in order, before the last one")
        return self

    @field_validator("client_draft_id")
    @classmethod
    def clean_client_draft_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not all(char.isalnum() or char == "-" for char in cleaned):
            raise ValueError("Invalid client draft id")
        return cleaned

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


class UserRouteEditablePlaceOut(BaseModel):
    """A stop as the editor draws it — enough to rebuild the card without a
    second request per place."""

    id: UUID
    name: str
    subtitle: str = ""
    lat: float | None = None
    lng: float | None = None


class UserRouteEditableOut(BaseModel):
    """A route's own content, as its author needs it to resume editing.

    `pace`, `filters` and `difficulty` live in `Route.accessibility` and are
    not part of the public route payload, so without this the editor could
    only be resumed from the device that still held the local draft
    (reported 2026-09-04).
    """

    id: UUID
    publication_status: RoutePublicationStatus
    name: str
    description: str
    places: list[UserRouteEditablePlaceOut]
    filters: list[str]
    pace: Literal["calm", "moderate", "active"]
    difficulty: int
    #: Spec 17: whether ``difficulty`` is the author's rating or the estimate.
    difficulty_manual: bool = False
    difficulty_auto: int | None = None
    #: The estimate's breakdown for the editor's hint.
    difficulty_breakdown: dict[str, object] | None = None
    media: list["UserRouteMediaOut"]
    updated_at: datetime
    #: Places after which the author ended a day; empty when split by norms.
    day_breaks: list[UUID] = Field(default_factory=list)


class UserRouteMediaSyncIn(BaseModel):
    """Ids of the already-stored files the editor still shows, in order.

    An empty list clears the gallery, same as the DELETE endpoint.
    """

    keep: list[UUID] = Field(default_factory=list, max_length=10)


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
    # Enough to render a small preview card when a map pin is tapped without
    # a second round trip per stop.
    place_short_description: str | None = None
    place_cover_url: str | None = None


class RouteMediaOut(BaseModel):
    id: UUID
    url: str
    kind: Literal["image", "video"]
    position: int


class RouteDraftPreviewIn(BaseModel):
    """Ordered points the author has placed so far, saved or not.

    Two points is enough to draw something useful — the author sees the road
    between start and finish before adding a single stop.
    """

    place_ids: list[UUID] = Field(min_length=2, max_length=22)
    transport_mode: Literal["walk", "car", "mixed", "bicycle", "public_transport"] = "walk"


class RouteDraftPreviewOut(BaseModel):
    """Road geometry for points that are not a saved route yet.

    ``preview_id`` addresses the cached geometry for the raster endpoint:
    a road line runs to hundreds of points, far past what a URL can carry.
    """

    preview_id: str
    geometry: "RouteGeometryOut | None" = None
    distance_meters: int = 0
    duration_seconds: int = 0
    provider: str
    # True when the provider could not be reached and the line is straight
    # segments between the points — still worth drawing on a real map, but
    # the client should not present it as a road.
    synthetic: bool = False


class RouteGeometryOut(BaseModel):
    """Provider geometry in a mobile-friendly GeoJSON subset."""

    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(default_factory=list)


class RouteRoutingOut(BaseModel):
    """Normalized routing provenance exposed without a provider payload/key."""

    provider: str | None = None
    synthetic: bool = False
    quality_status: RouteQualityStatus = "unknown"
    quality_policy_version: str | None = Field(default=None, max_length=32)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    movement_duration_seconds: int | None = Field(default=None, ge=0)
    visit_duration_minutes: int | None = Field(default=None, ge=0)
    transfer_duration_seconds: int | None = Field(default=None, ge=0)
    buffer_duration_seconds: int | None = Field(default=None, ge=0)
    total_duration_seconds: int | None = Field(default=None, ge=0)
    elevation_gain_meters: int | None = Field(default=None, ge=0)
    elevation_loss_meters: int | None = Field(default=None, ge=0)
    min_altitude_meters: int | None = None
    max_altitude_meters: int | None = None
    max_road_angle_degrees: float | None = Field(default=None, ge=0, le=90)
    road_types: list[str] = Field(default_factory=list, max_length=32)


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
    #: Spec 17: shown level 1..5, the estimate, whose rating is shown and how
    #: sure the estimate is. ``difficulty`` stays the word older apps read.
    difficulty_level: int | None = None
    difficulty_auto: int | None = None
    difficulty_source: Literal["auto", "author", "editorial", "legacy"] = "auto"
    difficulty_confidence: str | None = None
    transport_mode: str | None
    is_round_trip: bool
    suitable_for_children: bool | None
    pets_allowed: bool | None
    is_seaside: bool = False
    seasonality: list[str] | None
    stops_count: int
    author_label: str | None
    cover_image_url: str | None = None
    owner_user_id: UUID | None = None
    author_avatar_url: str | None = None
    author_is_expert: bool = False
    #: Travel rank of the owning user, resolved from ``travel_points``.
    #: ``None`` for editorial routes, which have no owning user.
    author_rank_title: str | None = None
    #: Mean of published, non-reply review ratings. ``None`` until the route
    #: has at least one — a card must not imply a score nobody has given.
    rating_average: float | None = None
    rating_count: int = 0


class RouteSegmentOut(BaseModel):
    """One stretch of a leg travelled one way (spec 14).

    ``role`` is ``main``, ``approach`` (walk from the car park up to a stop)
    or ``return`` (the same walk back to the car).
    """

    leg_index: int = Field(ge=0)
    seq: int = Field(ge=0)
    mode: str
    role: str
    origin: str
    distance_meters: int | None = None
    duration_seconds: int | None = None
    elevation_gain_meters: int | None = None
    geometry: RouteGeometryOut | None = None


class RouteDayOut(BaseModel):
    """A continuous run of stops walked or driven in one day (spec 14a)."""

    day_index: int = Field(ge=1)
    first_stop_id: UUID
    last_stop_id: UUID
    #: ``auto`` from the route's norms, ``manual`` once someone moved it.
    boundary_source: str = "auto"
    #: «Ночлег в районе: …»; none on the last day.
    overnight_note: str | None = None
    #: A leg longer than a whole day leads into it (spec 14, D4).
    overloaded: bool = False
    #: The day's estimated difficulty 1..5 (spec 17).
    difficulty_level: int | None = None


class RouteDaysIn(BaseModel):
    """Stops after which a day ends, in route order; empty for one day."""

    ends_after_stop_ids: list[UUID] = Field(default_factory=list, max_length=21)


class RouteDetailOut(RouteListItemOut):
    description: str | None
    budget_notes: str | None
    accessibility: dict[str, object] | None
    freshness_status: str
    geometry: RouteGeometryOut | None = None
    routing: RouteRoutingOut | None = None
    stops: list[RouteStopOut] = Field(default_factory=list)
    media: list[RouteMediaOut] = Field(default_factory=list)
    static_map_url: str | None = None
    #: ``walk``, ``car`` or ``mixed``; ``transport_mode`` keeps the stored
    #: spelling older apps understand (spec 14, R4).
    base_mode: str = "walk"
    needs_public_transport: bool = False
    #: Every leg's segments in order; empty until the route has them.
    segments: list[RouteSegmentOut] = Field(default_factory=list)
    #: Days in order; empty until the route has them, one for a short route.
    days: list[RouteDayOut] = Field(default_factory=list)


class RouteListOut(BaseModel):
    items: list[RouteListItemOut]
    total: int
    limit: int
    offset: int
