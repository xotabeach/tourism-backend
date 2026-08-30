"""Authenticated daily recommendation deck and skip feedback."""

from uuid import UUID

from fastapi import APIRouter, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.recommendations.application import service
from tourism_backend.modules.recommendations.application.schemas import (
    RecommendationDeckOut,
    RecommendationFeedbackIn,
    RecommendationFeedbackOut,
)

router = APIRouter(tags=["recommendations"])


@router.get("/routes/recommendations/today", response_model=RecommendationDeckOut)
async def get_today_recommendations(
    session: DbSession,
    user_id: CurrentUserId,
) -> RecommendationDeckOut:
    return await service.get_today_deck(session, user_id=user_id)


@router.post(
    "/routes/{route_id}/recommendation-feedback",
    response_model=RecommendationFeedbackOut,
    status_code=status.HTTP_200_OK,
)
async def post_recommendation_feedback(
    route_id: UUID,
    payload: RecommendationFeedbackIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> RecommendationFeedbackOut:
    return await service.record_feedback(
        session,
        user_id=user_id,
        route_id=route_id,
        payload=payload,
    )


@router.post(
    "/routes/recommendations/refresh",
    response_model=RecommendationDeckOut,
    status_code=status.HTTP_200_OK,
)
async def refresh_recommendations(
    session: DbSession,
    user_id: CurrentUserId,
) -> RecommendationDeckOut:
    return await service.refresh_today_deck(session, user_id=user_id)
