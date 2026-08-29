"""Public recommendation API schemas."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tourism_backend.modules.routes.application.schemas import RouteListItemOut

RecommendationAction = Literal["skip"]
ExplanationCode = Literal[
    "matches_interest",
    "nearby_exploration",
    "fresh_route",
    "popular_route",
    "cold_start",
]


class RecommendationFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RecommendationAction
    client_event_id: UUID
    deck_date: date | None = None
    ranker_version: str | None = Field(default=None, max_length=16)


class RecommendationFeedbackOut(BaseModel):
    route_id: UUID
    action: RecommendationAction
    client_event_id: UUID
    deck_date: date
    ranker_version: str
    created_at: datetime
    replayed: bool = False


class RecommendationCardOut(BaseModel):
    route: RouteListItemOut
    rank: int = Field(ge=1)
    score: float = Field(ge=0, le=1)
    explanation_code: ExplanationCode
    exploration: bool = False


class RecommendationDeckOut(BaseModel):
    deck_date: date
    ranker_version: str
    generated: bool
    items: list[RecommendationCardOut] = Field(default_factory=list, max_length=32)
    remaining: int = Field(ge=0)
