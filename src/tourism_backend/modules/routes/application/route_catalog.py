"""Public catalogue of routes: list items, covers, authors and ratings."""

from collections.abc import Sequence
from typing import Literal
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    exists,
    func,
    not_,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import EXPERT_RANK_ID, TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application.place_covers import generic_fallback_cover
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
from tourism_backend.modules.routes.application.difficulty import (
    level_from_legacy,
)
from tourism_backend.modules.routes.application.rejection import rejection_text
from tourism_backend.modules.routes.application.review_rules import partial_only_review
from tourism_backend.modules.routes.application.schemas import (
    RouteCatalogSort,
    RouteListItemOut,
    RouteListOut,
    RoutePublicationStatus,
    RouteSource,
)
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteReview,
    RouteStop,
)

_PUBLIC_CATALOG = (
    or_(Route.source == "editorial", Route.source == "user_created"),
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)


_PUBLIC_USER_OWNED = (
    Route.source == "user_created",
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)


def _has_unpublished_stop() -> Exists:
    return exists().where(
        RouteStop.route_id == Route.id,
        RouteStop.place_id == Place.id,
        Place.publication_status != "published",
    )


async def _stops_count_map(
    session: AsyncSession,
    route_ids: list[UUID],
) -> dict[UUID, int]:
    if not route_ids:
        return {}
    stmt = (
        select(RouteStop.route_id, func.count())
        .where(RouteStop.route_id.in_(route_ids))
        .group_by(RouteStop.route_id)
    )
    return {route_id: int(count) for route_id, count in (await session.execute(stmt)).all()}


async def _cover_urls_for_routes(
    session: AsyncSession,
    route_ids: list[UUID],
) -> dict[UUID, str]:
    """The route's own uploaded cover, then the stop photo the editors chose
    (``cover_place_image_id``, spec 16a D33), then the cover of the earliest
    stop that has an active cover photo."""
    if not route_ids:
        return {}
    direct_stmt = select(
        MediaAttachment.entity_id,
        MediaAttachment.public_path,
    ).where(
        MediaAttachment.entity_type == "route",
        MediaAttachment.entity_id.in_(route_ids),
        MediaAttachment.role == "cover",
        MediaAttachment.status == "active",
    )
    covers = {
        route_id: public_path
        for route_id, public_path in (await session.execute(direct_stmt)).all()
        if public_path
    }
    # Prefer media_attachments linked via place_images; fall back to source_url.
    attachment_url = func.coalesce(MediaAttachment.public_path, PlaceImage.source_url)
    chosen_ids = [route_id for route_id in route_ids if route_id not in covers]
    if chosen_ids:
        chosen_stmt = (
            select(Route.id, attachment_url)
            .join(PlaceImage, PlaceImage.id == Route.cover_place_image_id)
            .outerjoin(
                MediaAttachment,
                (MediaAttachment.id == PlaceImage.media_asset_id)
                & (MediaAttachment.status == "active"),
            )
            .where(
                Route.id.in_(chosen_ids),
                PlaceImage.status == "active",
                attachment_url.is_not(None),
            )
        )
        covers.update(dict((await session.execute(chosen_stmt)).tuples().all()))
    fallback_ids = [route_id for route_id in route_ids if route_id not in covers]
    if not fallback_ids:
        return covers
    ranked = (
        select(
            RouteStop.route_id.label("route_id"),
            attachment_url.label("source_url"),
            func.row_number()
            .over(
                partition_by=RouteStop.route_id,
                order_by=RouteStop.position,
            )
            .label("rn"),
        )
        .join(Place, Place.id == RouteStop.place_id)
        .join(PlaceImage, PlaceImage.place_id == Place.id)
        .outerjoin(
            MediaAttachment,
            (MediaAttachment.id == PlaceImage.media_asset_id)
            & (MediaAttachment.status == "active"),
        )
        .where(
            RouteStop.route_id.in_(fallback_ids),
            Place.publication_status == "published",
            PlaceImage.status == "active",
            PlaceImage.is_cover.is_(True),
            attachment_url.is_not(None),
        )
        .subquery()
    )
    stmt = select(ranked.c.route_id, ranked.c.source_url).where(ranked.c.rn == 1)
    covers.update(
        {
            route_id: source_url
            for route_id, source_url in (await session.execute(stmt)).all()
            if source_url
        }
    )
    still_missing = [route_id for route_id in route_ids if route_id not in covers]
    if still_missing:
        # Photo coverage is sparse today (import pipeline not fully run) —
        # never leave a route card blank, reuse any existing place photo.
        generic = await generic_fallback_cover(session)
        if generic:
            for route_id in still_missing:
                covers[route_id] = generic
    return covers


async def route_cover_urls(session: AsyncSession, route_ids: list[UUID]) -> dict[UUID, str]:
    """The cover a route shows in the catalog, for other modules to reuse."""
    return await _cover_urls_for_routes(session, route_ids)


async def _author_fields_for_routes(
    session: AsyncSession,
    routes: list[Route],
) -> dict[UUID, tuple[UUID | None, str | None, str | None, bool, str | None]]:
    """Map route.id -> owner identity fields used by every route card."""
    owner_ids = [route.owner_user_id for route in routes if route.owner_user_id is not None]
    users: dict[UUID, User] = {}
    if owner_ids:
        for user in (await session.scalars(select(User).where(User.id.in_(owner_ids)))).all():
            users[user.id] = user
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=list(users.keys()),
        role="avatar",
    )
    ranks = await _rank_titles(session, list(users.values()))
    result: dict[UUID, tuple[UUID | None, str | None, str | None, bool, str | None]] = {}
    for route in routes:
        owner_id = route.owner_user_id
        label: str | None
        avatar: str | None
        rank_title: str | None
        if owner_id is not None and owner_id in users:
            label = users[owner_id].display_name
            avatar = avatars.get(owner_id)
            is_expert = users[owner_id].is_expert
            # The editorial profile has no travel rank (spec 16, D6).
            rank_title = None if users[owner_id].is_system_account else ranks.get(owner_id)
        else:
            # Editorial route: no owning user, so no travel rank to show.
            label = route.author_label
            avatar = None
            is_expert = False
            rank_title = None
        result[route.id] = (owner_id, label, avatar, is_expert, rank_title)
    return result


async def _rank_titles(session: AsyncSession, users: list[User]) -> dict[UUID, str]:
    """Travel rank title per user, by ``travel_points``.

    Same resolution as the review services use, kept here rather than shared
    so the routes module does not reach into another module's application
    layer for it.
    """
    if not users:
        return {}
    ranks_sorted = sorted(
        (await session.scalars(select(TravelRank).where(TravelRank.id != EXPERT_RANK_ID))).all(),
        key=lambda rank: rank.min_points,
        reverse=True,
    )
    out: dict[UUID, str] = {}
    for user in users:
        title = "Новичок"
        if getattr(user, "is_expert", False):
            out[user.id] = "Эксперт"
            continue
        for rank in ranks_sorted:
            if user.travel_points >= rank.min_points:
                title = rank.title
                break
        out[user.id] = title
    return out


def _to_list_item(
    route: Route,
    stops_count: int,
    cover_image_url: str | None = None,
    *,
    owner_user_id: UUID | None = None,
    author_label: str | None = None,
    author_avatar_url: str | None = None,
    author_is_expert: bool = False,
    author_rank_title: str | None = None,
    rating: tuple[float | None, int] = (None, 0),
) -> RouteListItemOut:
    rating_average, rating_count = rating
    return RouteListItemOut(
        id=route.id,
        region_id=route.region_id,
        name=route.name,
        slug=route.slug,
        short_description=route.short_description,
        source=route.source,
        visibility=route.visibility,
        lifecycle_status=route.lifecycle_status,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        estimated_duration_minutes=route.estimated_duration_minutes,
        distance_meters=route.distance_meters,
        difficulty=route.difficulty,
        difficulty_level=route.difficulty_level,
        difficulty_auto=route.difficulty_auto,
        difficulty_source=type_cast(
            Literal["auto", "author", "editorial", "legacy"],
            route.difficulty_manual_by if route.difficulty_manual is not None else "auto",
        ),
        difficulty_confidence=route.difficulty_confidence,
        transport_mode=route.transport_mode,
        is_round_trip=route.is_round_trip,
        suitable_for_children=route.suitable_for_children,
        pets_allowed=route.pets_allowed,
        is_seaside=route.is_seaside,
        seasonality=route.seasonality,
        stops_count=stops_count,
        author_label=author_label if author_label is not None else route.author_label,
        cover_image_url=cover_image_url,
        owner_user_id=owner_user_id,
        author_avatar_url=author_avatar_url,
        author_is_expert=author_is_expert,
        author_is_editorial=route.source == "editorial",
        author_rank_title=author_rank_title,
        rating_average=rating_average,
        rating_count=rating_count,
        rejection_reason=author_rejection_text(route),
        badge=route_badge(route),
    )


def author_rejection_text(route: Route) -> str | None:
    """What the moderator asked to fix, for a route its author still has to resend.

    A published or queued route never carries it: the reason is cleared when
    the author sends the route again, and this guard keeps a stale one from
    leaking into the public catalog.
    """

    if route.publication_status not in {"rejected", "draft"}:
        return None
    return rejection_text(route.rejection_reason, route.moderator_note)


def route_badge(route: Route) -> Literal["popular", "editors_choice"] | None:
    """The catalog badge of a route (spec 19, D43)."""

    if route.is_popular:
        return "popular"
    if route.source == "editorial":
        return "editors_choice"
    return None


async def route_ratings(
    session: AsyncSession, route_ids: Sequence[UUID]
) -> dict[UUID, tuple[float | None, int]]:
    """Mean rating and count per route, in one pass over the page.

    Same conditions the review list already uses: published reviews only, and
    replies excluded — a reply carries a rating column it never meant.
    A route with no ratings is absent from the result, which the caller reads
    as "no score yet" rather than zero: an empty star says "bad", and that
    would be a lie about a route nobody has rated.
    """
    ids = [route_id for route_id in route_ids if route_id is not None]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                RouteReview.route_id,
                func.avg(RouteReview.rating),
                func.count(RouteReview.id),
            )
            .where(
                RouteReview.route_id.in_(ids),
                RouteReview.status == "published",
                RouteReview.reply_to_review_id.is_(None),
                # Same rule as the review list: stars of someone who walked
                # the route only in part stay out of the average.
                not_(partial_only_review()),
            )
            .group_by(RouteReview.route_id)
        )
    ).all()
    return {
        route_id: (round(float(average), 1) if average is not None else None, int(count))
        for route_id, average, count in rows
        if count
    }


async def list_catalog_items_by_ids(
    session: AsyncSession,
    route_ids: Sequence[UUID],
) -> dict[UUID, RouteListItemOut]:
    """Hydrate public catalog cards in the given id order."""

    if not route_ids:
        return {}
    unique_ids = list(dict.fromkeys(route_ids))
    routes = list((await session.scalars(select(Route).where(Route.id.in_(unique_ids)))).all())
    by_id = {route.id: route for route in routes}
    ordered = [by_id[route_id] for route_id in unique_ids if route_id in by_id]
    counts = await _stops_count_map(session, unique_ids)
    covers = await _cover_urls_for_routes(session, unique_ids)
    authors = await _author_fields_for_routes(session, ordered)
    ratings = await route_ratings(session, unique_ids)
    items: dict[UUID, RouteListItemOut] = {}
    for route in ordered:
        owner_id, label, avatar, is_expert, rank_title = authors[route.id]
        items[route.id] = _to_list_item(
            route,
            counts.get(route.id, 0),
            covers.get(route.id),
            owner_user_id=owner_id,
            author_label=label,
            author_avatar_url=avatar,
            author_is_expert=is_expert,
            author_rank_title=rank_title,
            rating=ratings.get(route.id, (None, 0)),
        )
    return items


async def _list_from_stmt(
    session: AsyncSession,
    stmt: Select[tuple[Route]],
    *,
    limit: int,
    offset: int,
    sort: RouteCatalogSort = "default",
) -> RouteListOut:
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())

    favorites_count = (
        select(func.count())
        .where(FavoriteRoute.route_id == Route.id)
        .correlate(Route)
        .scalar_subquery()
    )
    order_by = {
        "popular": (
            Route.popularity.desc(),
            favorites_count.desc(),
            Route.updated_at.desc(),
            Route.id,
        ),
        "recent": (Route.updated_at.desc(), Route.id),
        "name_asc": (Route.name, Route.id),
        "name_desc": (Route.name.desc(), Route.id),
        "date_newest": (Route.created_at.desc(), Route.id),
        "date_oldest": (Route.created_at, Route.id),
        "default": (Route.name, Route.id),
    }[sort]

    routes = list(
        (await session.scalars(stmt.order_by(*order_by).limit(limit).offset(offset))).all()
    )
    route_ids = [route.id for route in routes]
    counts = await _stops_count_map(session, route_ids)
    covers = await _cover_urls_for_routes(session, route_ids)
    authors = await _author_fields_for_routes(session, routes)
    ratings = await route_ratings(session, route_ids)
    items = []
    for route in routes:
        owner_id, label, avatar, is_expert, rank_title = authors[route.id]
        items.append(
            _to_list_item(
                route,
                counts.get(route.id, 0),
                covers.get(route.id),
                owner_user_id=owner_id,
                author_label=label,
                author_avatar_url=avatar,
                author_is_expert=is_expert,
                author_rank_title=rank_title,
                rating=ratings.get(route.id, (None, 0)),
            )
        )
    return RouteListOut(items=items, total=total, limit=limit, offset=offset)


async def list_routes(
    session: AsyncSession,
    *,
    region_slug: str | None,
    place_id: UUID | None,
    transport_mode: str | None,
    difficulty: str | None,
    q: str | None,
    source: RouteSource | None,
    sort: RouteCatalogSort,
    seaside: bool | None = None,
    difficulty_max: int | None = None,
    limit: int,
    offset: int,
) -> RouteListOut:
    stmt: Select[tuple[Route]] = select(Route).where(
        *_PUBLIC_CATALOG,
        ~_has_unpublished_stop(),
    )
    if region_slug:
        stmt = stmt.join(Region, Region.id == Route.region_id).where(Region.slug == region_slug)
    if place_id:
        routes_with_place = select(RouteStop.route_id).where(RouteStop.place_id == place_id)
        stmt = stmt.where(Route.id.in_(routes_with_place))
    if transport_mode:
        stmt = stmt.where(Route.transport_mode == transport_mode)
    if difficulty:
        # Older apps filter by the word: its range on the 1..5 scale, so a
        # level 5 route still shows under «сложный» (spec 17, D22).
        stmt = stmt.where(_difficulty_word_filter(difficulty))
    if difficulty_max is not None:
        stmt = stmt.where(Route.difficulty_level <= difficulty_max)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Route.name.ilike(pattern))
    if source:
        stmt = stmt.where(Route.source == source)
    if seaside is not None:
        stmt = stmt.where(Route.is_seaside.is_(seaside))

    return await _list_from_stmt(session, stmt, limit=limit, offset=offset, sort=sort)


async def list_public_routes_for_owner(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    limit: int,
    offset: int,
) -> RouteListOut:
    stmt: Select[tuple[Route]] = select(Route).where(
        *_PUBLIC_USER_OWNED,
        Route.owner_user_id == owner_user_id,
        ~_has_unpublished_stop(),
    )
    return await _list_from_stmt(session, stmt, limit=limit, offset=offset)


async def list_routes_for_owner(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    limit: int,
    offset: int,
) -> RouteListOut:
    stmt: Select[tuple[Route]] = select(Route).where(
        Route.source.in_(("user_created", "generated")),
        Route.owner_user_id == owner_user_id,
        Route.publication_status != "deleted",
    )
    return await _list_from_stmt(session, stmt, limit=limit, offset=offset)


def _difficulty_word_filter(word: str) -> ColumnElement[bool]:
    level = level_from_legacy(word)
    if level is None:
        return Route.difficulty == word
    if level <= 2:
        return Route.difficulty_level <= 2
    if level == 3:
        return Route.difficulty_level == 3
    return Route.difficulty_level >= 4
