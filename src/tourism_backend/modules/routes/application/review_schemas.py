from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReviewStatus = Literal["pending_review", "published", "rejected", "deleted"]


class RouteReviewCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=2000)
    rating: int = Field(ge=1, le=5)

    @field_validator("body")
    @classmethod
    def _trim_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


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


class RouteReviewListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RouteReviewOut]
    total: int
    average_rating: float | None
    rating_count: int


class MyRouteReviewListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RouteReviewOut]
