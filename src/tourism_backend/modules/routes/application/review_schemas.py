from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReviewStatus = Literal["pending_review", "published", "rejected", "deleted"]


class RouteReviewCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty means a bare star rating, allowed only after walking the route
    # (checked by the service, which knows about runs).
    body: str = Field(default="", max_length=2000)
    rating: int = Field(ge=1, le=5)
    reply_to_review_id: UUID | None = None

    @field_validator("body")
    @classmethod
    def _trim_body(cls, value: str) -> str:
        return value.strip()


class RouteReviewMediaOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    width: int | None
    height: int | None
    sort_order: int


class RouteReviewReplyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    author_user_id: str
    author_display_name: str
    body: str


class RouteReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    route_id: str
    author_user_id: str
    author_display_name: str
    author_rank_title: str
    author_avatar_url: str | None
    body: str
    rating: int
    status: ReviewStatus
    created_at: datetime
    media: list[RouteReviewMediaOut] = Field(default_factory=list)
    reply_to: RouteReviewReplyOut | None = None
    # The author has a completed run of this route.
    author_completed_route: bool = False


class RouteReviewListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RouteReviewOut]
    total: int
    average_rating: float | None
    rating_count: int


class MyRouteReviewListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RouteReviewOut]
