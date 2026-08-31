"""Public user profile reads — no PII beyond display name and media."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application.public_schemas import (
    PublicUserListOut,
    PublicUserOut,
)
from tourism_backend.modules.identity.application.travel_points import grant_due_travel_points
from tourism_backend.modules.identity.infrastructure.models import (
    EXPERT_RANK_ID,
    ProfileLike,
    TravelRank,
    User,
)
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.places.infrastructure.models import PlaceReview
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteRoutingSnapshot,
)
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteListOut
from tourism_backend.modules.routes.infrastructure.models import RouteReview


async def _rank_for_points(session: AsyncSession, points: int) -> TravelRank | None:
    rank: TravelRank | None = await session.scalar(
        select(TravelRank)
        .where(TravelRank.min_points <= points, TravelRank.id != EXPERT_RANK_ID)
        .order_by(TravelRank.min_points.desc())
        .limit(1)
    )
    return rank


async def _resolve_rank(session: AsyncSession, user: User) -> TravelRank | None:
    """Experts carry an admin-granted rank — never recomputed from points."""
    if user.is_expert:
        expert_rank = await session.get(TravelRank, EXPERT_RANK_ID)
        if expert_rank is not None:
            if user.rank_id != EXPERT_RANK_ID:
                user.rank_id = EXPERT_RANK_ID
            return expert_rank
    rank = await _rank_for_points(session, user.travel_points)
    if rank is not None and user.rank_id != rank.id:
        user.rank_id = rank.id
    return rank


async def _leaderboard_place(session: AsyncSession, points: int) -> int:
    ahead = int(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.travel_points > points,
                or_(User.rank_id.is_(None), User.rank_id != EXPERT_RANK_ID),
            )
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
    followers_count: int = 0,
    following_count: int = 0,
    completed_routes_count: int = 0,
    reviews_written_count: int = 0,
    total_distance_meters: int = 0,
) -> PublicUserOut:
    rank = await _resolve_rank(session, user)
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
        else (None if user.is_expert else await _leaderboard_place(session, user.travel_points)),
        liked_by_me=liked_by_me,
        is_expert=user.is_expert,
        followers_count=followers_count,
        following_count=following_count,
        completed_routes_count=completed_routes_count,
        reviews_written_count=reviews_written_count,
        total_distance_meters=total_distance_meters,
    )


async def _profile_activity_stats(session: AsyncSession, user_id: UUID) -> tuple[int, int, int]:
    """(completed_routes_count, reviews_written_count, total_distance_meters).

    Distance sums the immutable routing-snapshot distance recorded at
    execution start (the same source the travel-points algorithm uses,
    see route_execution.application.rewards) — never a live route field
    that could have changed since the user actually walked it.
    """
    completed_count, distance_sum = (
        await session.execute(
            select(
                func.count(RouteExecution.id),
                func.coalesce(func.sum(RouteRoutingSnapshot.distance_meters), 0),
            )
            .select_from(RouteExecution)
            .outerjoin(
                RouteRoutingSnapshot,
                RouteRoutingSnapshot.id == RouteExecution.routing_snapshot_id,
            )
            .where(RouteExecution.user_id == user_id, RouteExecution.status == "completed")
        )
    ).one()

    route_reviews = await session.scalar(
        select(func.count())
        .select_from(RouteReview)
        .where(RouteReview.author_user_id == user_id, RouteReview.status == "published")
    )
    place_reviews = await session.scalar(
        select(func.count())
        .select_from(PlaceReview)
        .where(PlaceReview.author_user_id == user_id, PlaceReview.status == "published")
    )
    return (
        int(completed_count or 0),
        int(route_reviews or 0) + int(place_reviews or 0),
        int(distance_sum or 0),
    )


async def _follow_counts(
    session: AsyncSession, user_ids: list[UUID]
) -> dict[UUID, tuple[int, int]]:
    if not user_ids:
        return {}
    followers_rows = (
        await session.execute(
            select(ProfileLike.liked_user_id, func.count())
            .where(ProfileLike.liked_user_id.in_(user_ids))
            .group_by(ProfileLike.liked_user_id)
        )
    ).all()
    following_rows = (
        await session.execute(
            select(ProfileLike.liker_id, func.count())
            .where(ProfileLike.liker_id.in_(user_ids))
            .group_by(ProfileLike.liker_id)
        )
    ).all()
    followers = {user_id: int(count) for user_id, count in followers_rows}
    following = {user_id: int(count) for user_id, count in following_rows}
    return {user_id: (followers.get(user_id, 0), following.get(user_id, 0)) for user_id in user_ids}


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
    counts = await _follow_counts(session, ids)
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            followers_count=counts.get(user.id, (0, 0))[0],
            following_count=counts.get(user.id, (0, 0))[1],
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
    # Эксперт is an admin-granted rank, not a points-earned one — excluding
    # it here is a rank check, not an ad-hoc flag filter, so it can't drift
    # out of sync with who's actually on that rank.
    non_expert = or_(User.rank_id.is_(None), User.rank_id != EXPERT_RANK_ID)
    total = int(await session.scalar(select(func.count()).select_from(User).where(non_expert)) or 0)
    users = list(
        (
            await session.scalars(
                select(User)
                .where(non_expert)
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
    counts = await _follow_counts(session, ids)
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            place=offset + index + 1,
            followers_count=counts.get(user.id, (0, 0))[0],
            following_count=counts.get(user.id, (0, 0))[1],
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
    counts = await _follow_counts(session, ids)
    items = [
        await _public_user(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            liked_by_me=True,
            followers_count=counts.get(user.id, (0, 0))[0],
            following_count=counts.get(user.id, (0, 0))[1],
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
    counts = await _follow_counts(session, [user.id])
    followers, following = counts.get(user.id, (0, 0))
    completed_routes, reviews_written, distance_meters = await _profile_activity_stats(
        session, user.id
    )
    result = await _public_user(
        session,
        user,
        avatar_url=avatars.get(user.id),
        cover_url=covers.get(user.id),
        liked_by_me=liked_by_me,
        followers_count=followers,
        following_count=following,
        completed_routes_count=completed_routes,
        reviews_written_count=reviews_written,
        total_distance_meters=distance_meters,
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
