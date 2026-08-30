from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PlaceCatalogSort = Literal[
    "default",
    "name_asc",
    "name_desc",
    "date_newest",
    "date_oldest",
]


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    slug: str
    name: str
    description: str | None
    icon_key: str | None
    sort_order: int
    status: str


class PlaceListItemOut(BaseModel):
    id: UUID
    region_id: UUID
    locality_id: UUID | None
    name: str
    slug: str
    short_description: str | None
    lng: float
    lat: float
    difficulty: str | None
    is_paid: bool
    payment_status: str
    is_suitable_for_children: bool | None
    is_suitable_for_pets: bool | None
    recommended_visit_minutes: int | None
    typical_crowding: str = "unknown"
    price_min_amount: int | None = None
    price_max_amount: int | None = None
    price_currency: str = "RUB"
    access_transport: list[str] | None = None
    parking_available: bool | None = None
    publication_status: str
    categories: list[CategoryOut] = Field(default_factory=list)
    cover_image_url: str | None = None


class PlaceEntranceOut(BaseModel):
    id: UUID
    name: str
    entrance_type: str
    is_primary: bool
    lng: float
    lat: float
    address_hint: str | None


class PlaceDetailOut(PlaceListItemOut):
    image_urls: list[str] = Field(default_factory=list)
    description: str | None
    address: str | None
    contact_phone: str | None
    website_url: str | None
    accessibility: dict[str, Any] | None
    recommended_equipment: list[str] | None
    seasonality: list[str] | None
    price_notes: str | None
    safety_warnings: list[str] | None
    temporary_closure_status: str | None
    temporary_closure_reason: str | None
    freshness_status: str
    source_name: str | None
    source_license: str | None
    data_quality_status: str
    content_enrichment_status: str = "missing"
    proposed_slug: str | None = None
    primary_entrance: PlaceEntranceOut | None = None
    static_map_url: str | None = None


class PlaceListOut(BaseModel):
    items: list[PlaceListItemOut]
    total: int
    limit: int
    offset: int
