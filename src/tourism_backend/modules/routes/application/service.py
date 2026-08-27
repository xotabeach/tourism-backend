from datetime import UTC, datetime
from typing import cast as type_cast
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, cast, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from tourism_backend.api.errors import AppError
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import EXPERT_RANK_ID, TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application.place_covers import generic_fallback_cover
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
from tourism_backend.modules.routes.application.media import SavedRouteMedia
from tourism_backend.modules.routes.application.schemas import (
    RouteCatalogSort,
    RouteDetailOut,
    RouteListItemOut,
    RouteListOut,
    RouteMediaOut,
    RoutePublicationStatus,
    RouteSource,
    RouteStopOut,
    UserRouteDraftIn,
    UserRouteDraftOut,
    UserRouteMediaOut,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

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
    """Prefer cover of the earliest stop that has an active cover photo."""
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
    fallback_ids = [route_id for route_id in route_ids if route_id not in covers]
    if not fallback_ids:
        return covers
    # Prefer media_attachments linked via place_images; fall back to source_url.
    attachment_url = func.coalesce(MediaAttachment.public_path, PlaceImage.source_url)
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
            rank_title = ranks.get(owner_id)
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
) -> RouteListItemOut:
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
        transport_mode=route.transport_mode,
        is_round_trip=route.is_round_trip,
        suitable_for_children=route.suitable_for_children,
        pets_allowed=route.pets_allowed,
        seasonality=route.seasonality,
        stops_count=stops_count,
        author_label=author_label if author_label is not None else route.author_label,
        cover_image_url=cover_image_url,
        owner_user_id=owner_user_id,
        author_avatar_url=author_avatar_url,
        author_is_expert=author_is_expert,
        author_rank_title=author_rank_title,
    )


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
        "popular": (favorites_count.desc(), Route.updated_at.desc(), Route.id),
        "recent": (Route.updated_at.desc(), Route.id),
        "default": (Route.name, Route.id),
    }[sort]

    routes = list(
        (await session.scalars(stmt.order_by(*order_by).limit(limit).offset(offset))).all()
    )
    route_ids = [route.id for route in routes]
    counts = await _stops_count_map(session, route_ids)
    covers = await _cover_urls_for_routes(session, route_ids)
    authors = await _author_fields_for_routes(session, routes)
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
        stmt = stmt.where(Route.difficulty == difficulty)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Route.name.ilike(pattern))
    if source:
        stmt = stmt.where(Route.source == source)

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


async def _route_detail_from_model(
    session: AsyncSession,
    route: Route,
    *,
    public_stops_only: bool,
) -> RouteDetailOut:
    stops_stmt = (
        select(
            RouteStop,
            Place,
            ST_X(cast(Place.location, Geometry)),
            ST_Y(cast(Place.location, Geometry)),
        )
        .join(Place, Place.id == RouteStop.place_id)
        .where(RouteStop.route_id == route.id)
        .order_by(RouteStop.position)
    )
    if public_stops_only:
        stops_stmt = stops_stmt.where(Place.publication_status == "published")
    stops_rows = (await session.execute(stops_stmt)).all()
    attachments = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == route.id,
                    MediaAttachment.status == "active",
                )
                .order_by(MediaAttachment.sort_order, MediaAttachment.id)
            )
        ).all()
    )
    media = [
        RouteMediaOut(
            id=attachment.id,
            url=attachment.public_path,
            kind="video" if (attachment.content_type or "").startswith("video/") else "image",
            position=attachment.sort_order,
        )
        for attachment in attachments
    ]

    stops: list[RouteStopOut] = [
        RouteStopOut(
            id=stop.id,
            position=stop.position,
            place_id=place.id,
            place_name=place.name,
            place_slug=place.slug,
            visit_duration_minutes=stop.visit_duration_minutes,
            note=stop.note,
            is_optional=stop.is_optional,
            lng=float(lng) if lng is not None else None,
            lat=float(lat) if lat is not None else None,
        )
        for stop, place, lng, lat in stops_rows
    ]
    covers = await _cover_urls_for_routes(session, [route.id])
    authors = await _author_fields_for_routes(session, [route])
    owner_id, label, avatar, is_expert, rank_title = authors[route.id]
    base = _to_list_item(
        route,
        len(stops),
        covers.get(route.id),
        owner_user_id=owner_id,
        author_label=label,
        author_avatar_url=avatar,
        author_is_expert=is_expert,
        author_rank_title=rank_title,
    )
    return RouteDetailOut(
        **base.model_dump(),
        description=route.description,
        budget_notes=route.budget_notes,
        accessibility=route.accessibility,
        freshness_status=route.freshness_status,
        stops=stops,
        media=media,
    )


async def get_route(session: AsyncSession, route_id: UUID) -> RouteDetailOut:
    route = await session.scalar(
        select(Route).where(
            Route.id == route_id,
            *_PUBLIC_CATALOG,
            ~_has_unpublished_stop(),
        )
    )
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    return await _route_detail_from_model(session, route, public_stops_only=True)


async def get_owned_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> RouteDetailOut:
    route = await session.scalar(
        select(Route).where(
            Route.id == route_id,
            Route.owner_user_id == owner_user_id,
            Route.source.in_(("user_created", "generated")),
            Route.publication_status != "deleted",
        )
    )
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    return await _route_detail_from_model(session, route, public_stops_only=False)


def _difficulty_name(value: int) -> str:
    if value <= 2:
        return "easy"
    if value == 3:
        return "moderate"
    return "hard"


async def _owned_editable_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> Route:
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_user_id
        or route.source not in {"user_created", "generated"}
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if route.publication_status not in {"draft", "rejected"}:
        raise AppError(
            code="route_not_editable",
            message="Route cannot be edited in its current status",
            status_code=409,
        )
    return route


async def save_user_route_draft(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    payload: UserRouteDraftIn,
) -> UserRouteDraftOut:
    places = list(
        (
            await session.scalars(
                select(Place).where(
                    Place.id.in_(payload.place_ids),
                    Place.publication_status == "published",
                )
            )
        ).all()
    )
    place_by_id = {place.id: place for place in places}
    if len(place_by_id) != len(payload.place_ids):
        raise AppError(
            code="invalid_route_place",
            message="One or more route places are unavailable",
            status_code=400,
        )
    region_ids = {place.region_id for place in places}
    if len(region_ids) != 1:
        raise AppError(
            code="invalid_route_region",
            message="All route places must belong to one region",
            status_code=400,
        )

    now = datetime.now(UTC)
    if payload.route_id is None:
        route_id = uuid4()
        route = Route(
            id=route_id,
            region_id=next(iter(region_ids)),
            owner_user_id=owner_user_id,
            name=payload.name,
            slug=f"user-{route_id.hex}",
            source="user_created",
            visibility="private",
            lifecycle_status="draft",
            publication_status="draft",
            freshness_status="unknown",
            created_at=now,
            updated_at=now,
        )
        session.add(route)
        await session.flush()
    else:
        route = await _owned_editable_route(
            session,
            route_id=payload.route_id,
            owner_user_id=owner_user_id,
        )
        await session.execute(delete(RouteStop).where(RouteStop.route_id == route.id))

    route.region_id = next(iter(region_ids))
    route.name = payload.name
    route.short_description = payload.description[:240] or None
    route.description = payload.description or None
    route.visibility = "private"
    route.lifecycle_status = "draft"
    route.publication_status = "draft"
    route.difficulty = _difficulty_name(payload.difficulty)
    route.transport_mode = "walking"
    route.suitable_for_children = "С детьми" in payload.filters
    route.accessibility = {
        "travel_pace": payload.pace,
        "filters": payload.filters,
        "difficulty_level": payload.difficulty,
    }
    route.updated_at = now

    for position, place_id in enumerate(payload.place_ids, start=1):
        session.add(
            RouteStop(
                id=uuid4(),
                route_id=route.id,
                place_id=place_id,
                position=position,
                is_optional=False,
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()
    await session.refresh(route)
    return UserRouteDraftOut(
        id=route.id,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        updated_at=route.updated_at,
    )


async def submit_user_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteDraftOut:
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    media_count = int(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route.id,
                MediaAttachment.status == "active",
            )
        )
        or 0
    )
    stops_count = int(
        await session.scalar(select(func.count()).where(RouteStop.route_id == route.id)) or 0
    )
    if media_count == 0:
        raise AppError(
            code="route_media_required",
            message="At least one route photo or video is required",
            status_code=400,
        )
    if stops_count < 2:
        raise AppError(
            code="route_points_required",
            message="Start and finish are required",
            status_code=400,
        )
    route.publication_status = "pending_review"
    route.visibility = "private"
    route.lifecycle_status = "draft"
    route.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(route)
    return UserRouteDraftOut(
        id=route.id,
        publication_status=type_cast(RoutePublicationStatus, route.publication_status),
        updated_at=route.updated_at,
    )


async def discard_user_route_draft(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    now = datetime.now(UTC)
    route.publication_status = "deleted"
    route.lifecycle_status = "archived"
    route.visibility = "private"
    route.updated_at = now
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route_id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )
    await session.commit()


async def clear_user_route_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route_id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=datetime.now(UTC))
    )
    await session.commit()


async def ensure_user_route_editable(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )


async def add_user_route_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
    position: int,
    saved: SavedRouteMedia,
) -> UserRouteMediaOut:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    active_count = int(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route_id,
                MediaAttachment.status == "active",
            )
        )
        or 0
    )
    if active_count >= 10:
        raise AppError(
            code="route_media_limit",
            message="A route can contain at most 10 media files",
            status_code=400,
        )

    has_cover = bool(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route_id,
                MediaAttachment.role == "cover",
                MediaAttachment.status == "active",
            )
        )
    )
    role = "cover" if saved.kind == "image" and not has_cover else "gallery"
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="route",
        entity_id=route_id,
        role=role,
        storage_key=saved.storage_key,
        public_path=saved.public_path,
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
        status="active",
        uploaded_by_user_id=owner_user_id,
        sort_order=position,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(attachment)
    await session.commit()
    return UserRouteMediaOut(
        id=attachment.id,
        public_path=attachment.public_path,
        kind=saved.kind,
        position=position,
    )
