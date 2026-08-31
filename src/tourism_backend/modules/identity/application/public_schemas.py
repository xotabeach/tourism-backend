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
    # Experts are intentionally outside the public leaderboard.
    leaderboard_place: int | None = None
    liked_by_me: bool = False
    is_expert: bool = False
    followers_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)
    # Only computed for a single-profile fetch (get_public_user) — search/
    # leaderboard/subscriptions list rows reuse this same schema but stay at
    # the default 0 rather than pay for a per-row aggregation query.
    completed_routes_count: int = Field(default=0, ge=0)
    reviews_written_count: int = Field(default=0, ge=0)
    total_distance_meters: int = Field(default=0, ge=0)


class PublicUserListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PublicUserOut]
    total: int
