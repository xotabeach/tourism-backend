"""Public user profile read models."""

from pydantic import BaseModel, ConfigDict, Field


class PublicUserOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    avatar_url: str | None = None
    cover_url: str | None = None
    travel_points: int = 0
    rank_slug: str = "novice"
    rank_title: str = "Новичок"
    next_rank_points: int = 1000
    leaderboard_place: int = 0
    liked_by_me: bool = False
    followers_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)


class PublicUserListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PublicUserOut]
    total: int
