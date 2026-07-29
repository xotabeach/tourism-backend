from pydantic import BaseModel, ConfigDict, Field


class FavoritesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_ids: list[str] = Field(default_factory=list)
    route_ids: list[str] = Field(default_factory=list)
