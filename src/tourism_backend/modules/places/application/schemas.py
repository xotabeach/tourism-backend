from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    is_suitable_for_children: bool | None
    publication_status: str
    categories: list[CategoryOut] = Field(default_factory=list)


class PlaceEntranceOut(BaseModel):
    id: UUID
    name: str
    entrance_type: str
    is_primary: bool
    lng: float
    lat: float
    address_hint: str | None


class PlaceDetailOut(PlaceListItemOut):
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
    primary_entrance: PlaceEntranceOut | None = None


class PlaceListOut(BaseModel):
    items: list[PlaceListItemOut]
    total: int
    limit: int
    offset: int
