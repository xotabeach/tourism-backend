from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CountryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    slug: str
    name: str
    default_locale: str
    timezone: str
    status: str


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    country_id: UUID
    name: str
    slug: str
    administrative_code: str | None
    timezone: str
    status: str
    center_lng: float | None = None
    center_lat: float | None = None


class LocalityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    region_id: UUID
    parent_locality_id: UUID | None
    name: str
    slug: str
    type: str
    status: str
    center_lng: float | None = None
    center_lat: float | None = None
