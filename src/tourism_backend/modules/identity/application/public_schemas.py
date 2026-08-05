"""Public user profile read models."""

from pydantic import BaseModel, ConfigDict


class PublicUserOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    avatar_url: str | None = None
    cover_url: str | None = None
    travel_points: int = 0
    liked_by_me: bool = False


class PublicUserListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PublicUserOut]
    total: int
