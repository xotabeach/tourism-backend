"""Delayed +5 travel-point awards for profile likes and route favorites."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.identity.infrastructure.models import ProfileLike, User
from tourism_backend.modules.routes.infrastructure.models import Route

AWARD_DELAY = timedelta(hours=6)
AWARD_POINTS = 5


async def grant_due_travel_points(session: AsyncSession) -> int:
    """Idempotently grant points for reactions older than [AWARD_DELAY]."""
    now = datetime.now(UTC)
    cutoff = now - AWARD_DELAY
    granted = 0

    likes = await session.execute(
        select(ProfileLike).where(
            ProfileLike.awarded_at.is_(None),
            ProfileLike.created_at <= cutoff,
        )
    )
    for like in likes.scalars().all():
        beneficiary = await session.get(User, like.liked_user_id)
        if beneficiary is None:
            continue
        beneficiary.travel_points += AWARD_POINTS
        like.awarded_at = now
        granted += 1

    favs = await session.execute(
        select(FavoriteRoute, Route)
        .join(Route, Route.id == FavoriteRoute.route_id)
        .where(
            FavoriteRoute.author_points_awarded_at.is_(None),
            FavoriteRoute.created_at <= cutoff,
            Route.owner_user_id.is_not(None),
        )
    )
    for fav, route in favs.all():
        owner_id = route.owner_user_id
        if owner_id is None or owner_id == fav.user_id:
            # Self-favorite never awards.
            fav.author_points_awarded_at = now
            continue
        beneficiary = await session.get(User, owner_id)
        if beneficiary is None:
            continue
        beneficiary.travel_points += AWARD_POINTS
        fav.author_points_awarded_at = now
        granted += 1

    if granted:
        await session.commit()
    return granted
