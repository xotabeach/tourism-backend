"""Public profile read APIs + authenticated profile likes."""

from uuid import UUID

from fastapi import APIRouter, Query, status

from tourism_backend.api.deps import CurrentUserId, DbSession, OptionalCurrentUserId
from tourism_backend.modules.identity.application import achievements, profile_likes, public_service
from tourism_backend.modules.identity.application.achievement_schemas import AchievementListOut
from tourism_backend.modules.identity.application.public_schemas import (
    PublicUserListOut,
    PublicUserOut,
)
from tourism_backend.modules.routes.application.schemas import RouteListOut

router = APIRouter(tags=["users"])


@router.get("/users/search", response_model=PublicUserListOut)
async def search_public_users(
    session: DbSession,
    q: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=8, ge=1, le=20),
) -> PublicUserListOut:
    return await public_service.search_public_users(session, query=q, limit=limit)


@router.get("/users/leaderboard", response_model=PublicUserListOut)
async def get_users_leaderboard(
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> PublicUserListOut:
    return await public_service.list_leaderboard(session, limit=limit, offset=offset)


@router.get("/users/subscriptions", response_model=PublicUserListOut)
async def list_profile_subscriptions(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=100),
) -> PublicUserListOut:
    return await public_service.list_profile_subscriptions(
        session,
        user_id=user_id,
        limit=limit,
    )


@router.get("/users/{user_id}", response_model=PublicUserOut)
async def get_public_user(
    session: DbSession,
    user_id: UUID,
    viewer_id: OptionalCurrentUserId,
) -> PublicUserOut:
    return await public_service.get_public_user(session, user_id, viewer_id=viewer_id)


@router.get("/users/{user_id}/routes", response_model=RouteListOut)
async def get_public_user_routes(
    session: DbSession,
    user_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteListOut:
    return await public_service.list_public_user_routes(
        session,
        user_id,
        limit=limit,
        offset=offset,
    )


@router.get("/users/{user_id}/achievements", response_model=AchievementListOut)
async def get_public_user_achievements(
    session: DbSession,
    user_id: UUID,
) -> AchievementListOut:
    return await achievements.list_for_user(session, user_id)


@router.put("/users/{user_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def put_profile_like(
    session: DbSession,
    user_id: UUID,
    actor_id: CurrentUserId,
) -> None:
    await profile_likes.like_profile(session, actor_id=actor_id, target_user_id=user_id)


@router.delete("/users/{user_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile_like(
    session: DbSession,
    user_id: UUID,
    actor_id: CurrentUserId,
) -> None:
    await profile_likes.unlike_profile(session, actor_id=actor_id, target_user_id=user_id)
