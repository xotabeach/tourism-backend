"""Public user profile reads — no PII beyond display name and media."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application.public_schemas import (
    PublicUserListOut,
    PublicUserOut,
)
from tourism_backend.modules.identity.application.travel_points import grant_due_travel_points
from tourism_backend.modules.identity.infrastructure.models import ProfileLike, TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteListOut


async def _rank_for_points(session: AsyncSession, points: int) -> TravelRank | None:
    rank: TravelRank | None = await session.scalar(
        select(TravelRank)
        .where(TravelRank.min_points <= points)
        .order_by(TravelRank.min_points.desc())
        .limit(1)
    )
    return rank


async def _leaderboard_place(session: AsyncSession, points: int) -> int:
    ahead = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.travel_points > points)
        )
        or 0
    )
    return ahead + 1


async def _public_user(
    session: AsyncSession,
    user: User,
    *,
    avatar_url: str | None = None,
    cover_url: str | None = None,
    liked_by_me: bool = False,
    place: int | None = None,
) -> PublicUserOut:
    rank = await _rank_for_points(session, user.travel_points)
    if rank is not None and user.rank_id != rank.id:
        user.rank_id = rank.id
    return PublicUserOut(
        id=str(user.id),
        display_name=user.display_name,
        avatar_url=avatar_url,
        cover_url=cover_url,
        travel_points=user.travel_points,
        rank_slug=rank.slug if rank is not None else "novice",
        rank_title=rank.title if rank is not None else "Новичок",
        next_rank_points=rank.next_rank_points if rank is not None else 1_000,
        leaderboard_place=place
        if place is not None
        else await _leaderboard_place(session, user.travel_points),
        liked_by_me=liked_by_me,
    )


async def search_public_users(
    session: AsyncSession,
    *,
    query: str,
    limit: int,
) -> PublicUserListOut:
    cleaned = query.strip()
    if len(cleaned) < 2:
        return PublicUserListOut(items=[], total=0)

    predicate = User.display_name.ilike(f"%{cleaned}%")
    total = int(await session.scalar(select(func.count()).select_from(User).where(predicate)) or 0)
    users = list(
        (
            await session.scalars(
                select(User)
                .where(predicate)
                .order_by(User.travel_points.desc(), User.display_name, User.id)
                .limit(limit)
            )
        ).all()
    )
    ids = [user.id for user in users]
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=ids,
        role="avatar",
    )
    covers = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=ids,
        role="cover",
    )
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
        )
        for user in users
    ]
    await session.commit()
    return PublicUserListOut(items=items, total=total)


async def list_leaderboard(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> PublicUserListOut:
    await grant_due_travel_points(session)
    total = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    users = list(
        (
            await session.scalars(
                select(User)
                .order_by(User.travel_points.desc(), User.created_at, User.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    ids = [user.id for user in users]
    avatars = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=ids, role="avatar"
    )
    covers = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=ids, role="cover"
    )
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            place=offset + index + 1,
        )
        for index, user in enumerate(users)
    ]
    await session.commit()
    return PublicUserListOut(items=items, total=total)


async def list_profile_subscriptions(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int,
) -> PublicUserListOut:
    predicate = ProfileLike.liker_id == user_id
    total = int(
        await session.scalar(select(func.count()).select_from(ProfileLike).where(predicate)) or 0
    )
    users = list(
        (
            await session.scalars(
                select(User)
                .join(ProfileLike, ProfileLike.liked_user_id == User.id)
                .where(predicate)
                .order_by(ProfileLike.created_at.desc(), User.id)
                .limit(limit)
            )
        ).all()
    )
    ids = [user.id for user in users]
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=ids,
        role="avatar",
    )
    covers = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=ids,
        role="cover",
    )
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            liked_by_me=True,
        )
        for user in users
    ]
    await session.commit()
    return PublicUserListOut(items=items, total=total)


async def get_public_user(
    session: AsyncSession,
    user_id: UUID,
    *,
    viewer_id: UUID | None = None,
) -> PublicUserOut:
    await grant_due_travel_points(session)
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)

    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="avatar",
    )
    covers = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="cover",
    )
    liked_by_me = False
    if viewer_id is not None and viewer_id != user_id:
        liked_by_me = (await session.get(ProfileLike, (viewer_id, user_id))) is not None
    result = await _public_user(
        session,
        user,
        avatar_url=avatars.get(user.id),
        cover_url=covers.get(user.id),
        liked_by_me=liked_by_me,
    )
    await session.commit()
    return result


async def list_public_user_routes(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    offset: int,
) -> RouteListOut:
    # Ensure user exists (404) before listing routes.
    exists = await session.scalar(select(User.id).where(User.id == user_id))
    if exists is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    return await routes_service.list_public_routes_for_owner(
        session,
        owner_user_id=user_id,
        limit=limit,
        offset=offset,
    )
