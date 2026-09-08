import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from typing import cast as type_cast
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y, ST_AsGeoJSON
from redis.asyncio import Redis
from sqlalchemy import Select, cast, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import EXPERT_RANK_ID, TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application import place_covers
from tourism_backend.modules.places.application.place_covers import generic_fallback_cover
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    TransportMode,
)
from tourism_backend.modules.route_builder.infrastructure.routing_factory import (
    get_routing_provider,
)
from tourism_backend.modules.route_builder.infrastructure.routing_stub import (
    StubRoutingProvider,
)
from tourism_backend.modules.routes.application.media import SavedRouteMedia
from tourism_backend.modules.routes.application.schemas import (
    RouteCatalogSort,
    RouteDetailOut,
    RouteDraftPreviewIn,
    RouteDraftPreviewOut,
    RouteGeometryOut,
    RouteListItemOut,
    RouteListOut,
    RouteMediaOut,
    RoutePublicationStatus,
    RouteQualityStatus,
    RouteRoutingOut,
    RouteSource,
    RouteStopOut,
    UserRouteDraftIn,
    UserRouteDraftOut,
    UserRouteEditableOut,
    UserRouteEditablePlaceOut,
    UserRouteMediaOut,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview, RouteStop

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
        rating_average=rating_average,
        rating_count=rating_count,
    )


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
        "popular": (favorites_count.desc(), Route.updated_at.desc(), Route.id),
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

    stop_covers = await place_covers.covers_for_places(
        session,
        [place.id for _stop, place, _lng, _lat in stops_rows],
    )
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
            place_short_description=place.short_description,
            place_cover_url=stop_covers.get(place.id),
        )
        for stop, place, lng, lat in stops_rows
    ]
    covers = await _cover_urls_for_routes(session, [route.id])
    geometry = await _geometry_for_route(session, route.id)
    routing = _routing_for_route(route.accessibility)
    authors = await _author_fields_for_routes(session, [route])
    ratings = await route_ratings(session, [route.id])
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
        rating=ratings.get(route.id, (None, 0)),
    )
    return RouteDetailOut(
        **base.model_dump(),
        description=route.description,
        budget_notes=route.budget_notes,
        accessibility=route.accessibility,
        freshness_status=route.freshness_status,
        geometry=geometry,
        routing=routing,
        stops=stops,
        media=media,
        static_map_url=f"/api/v1/maps/static/route/{route.id}",
    )


async def _geometry_for_route(
    session: AsyncSession,
    route_id: UUID,
) -> RouteGeometryOut | None:
    """Read only a validated LineString, never the provider's raw response."""

    raw = await session.scalar(select(ST_AsGeoJSON(Route.geometry)).where(Route.id == route_id))
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "LineString":
        return None
    raw_coordinates = payload.get("coordinates")
    if not isinstance(raw_coordinates, list):
        return None
    coordinates: list[tuple[float, float]] = []
    for pair in raw_coordinates:
        if not isinstance(pair, list) or len(pair) < 2:
            continue
        lon, lat = pair[0], pair[1]
        if (
            isinstance(lon, bool)
            or isinstance(lat, bool)
            or not isinstance(lon, (int, float))
            or not isinstance(lat, (int, float))
            or not math.isfinite(float(lon))
            or not math.isfinite(float(lat))
            or not -180 <= float(lon) <= 180
            or not -90 <= float(lat) <= 90
        ):
            continue
        coordinates.append((float(lon), float(lat)))
    if len(coordinates) < 2:
        return None
    return RouteGeometryOut(coordinates=coordinates)


def _routing_for_route(value: object) -> RouteRoutingOut | None:
    """Map the small normalized routing metadata kept in accessibility JSON."""

    if not isinstance(value, dict):
        return None
    raw = value.get("routing")
    if not isinstance(raw, dict):
        return None
    warnings = [item for item in raw.get("warnings", []) if isinstance(item, str)]
    road_types = [item for item in raw.get("road_types", []) if isinstance(item, str)]
    provider = raw.get("provider") if isinstance(raw.get("provider"), str) else None
    allowed_quality_statuses = {
        "unverified",
        "checking",
        "verified",
        "verified_with_warnings",
        "needs_review",
        "unusable",
    }
    raw_quality_status = raw.get("quality_status")
    quality_status: RouteQualityStatus = (
        type_cast(RouteQualityStatus, raw_quality_status)
        if isinstance(raw_quality_status, str) and raw_quality_status in allowed_quality_statuses
        else "unknown"
    )
    quality_policy_version = (
        raw.get("quality_policy_version")
        if isinstance(raw.get("quality_policy_version"), str)
        else None
    )
    movement_duration: int | None = None
    visit_duration: int | None = None
    transfer_duration: int | None = None
    buffer_duration: int | None = None
    total_duration: int | None = None
    elevation_gain: int | None = None
    elevation_loss: int | None = None
    for field in (
        "movement_duration_seconds",
        "visit_duration_minutes",
        "transfer_duration_seconds",
        "buffer_duration_seconds",
        "total_duration_seconds",
        "elevation_gain_meters",
        "elevation_loss_meters",
    ):
        candidate = raw.get(field)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            if field == "movement_duration_seconds":
                movement_duration = candidate
            elif field == "visit_duration_minutes":
                visit_duration = candidate
            elif field == "transfer_duration_seconds":
                transfer_duration = candidate
            elif field == "buffer_duration_seconds":
                buffer_duration = candidate
            elif field == "total_duration_seconds":
                total_duration = candidate
            elif field == "elevation_gain_meters":
                elevation_gain = candidate
            else:
                elevation_loss = candidate
    min_altitude = _optional_int(raw.get("min_altitude_meters"))
    max_altitude = _optional_int(raw.get("max_altitude_meters"))
    max_road_angle: float | None = None
    angle = raw.get("max_road_angle_degrees")
    if (
        isinstance(angle, (int, float))
        and not isinstance(angle, bool)
        and math.isfinite(float(angle))
        and 0 <= float(angle) <= 90
    ):
        max_road_angle = float(angle)
    return RouteRoutingOut(
        provider=provider,
        synthetic=bool(raw.get("synthetic", False)),
        quality_status=quality_status,
        quality_policy_version=quality_policy_version,
        warnings=warnings[:32],
        movement_duration_seconds=movement_duration,
        visit_duration_minutes=visit_duration,
        transfer_duration_seconds=transfer_duration,
        buffer_duration_seconds=buffer_duration,
        total_duration_seconds=total_duration,
        elevation_gain_meters=elevation_gain,
        elevation_loss_meters=elevation_loss,
        min_altitude_meters=min_altitude,
        max_altitude_meters=max_altitude,
        max_road_angle_degrees=max_road_angle,
        road_types=road_types[:32],
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return int(round(value))


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


# A published route is editable, but saving sends it back through review —
# same rule as articles: otherwise moderation is bypassed by publishing
# something plain and swapping the stops afterwards. `pending_review` is
# editable in place; nothing has been approved yet.
_EDITABLE_ROUTE_STATUSES = frozenset({"draft", "rejected", "pending_review", "published"})


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
    allowed: frozenset[str] = _EDITABLE_ROUTE_STATUSES,
) -> Route:
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_user_id
        or route.source not in {"user_created", "generated"}
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if route.publication_status not in allowed:
        raise AppError(
            code="route_not_editable",
            message="Route cannot be edited in its current status",
            status_code=409,
        )
    return route


async def get_user_route_for_edit(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteEditableOut:
    """The author's own route, in the shape the editor needs to resume.

    Pace, filters and difficulty are stored inside `accessibility` and are
    not part of the public payload, so the editor could previously only be
    resumed from the device that still held the local draft.
    """
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    # Places come back with their card data, so the editor can redraw the
    # stop list without a request per place.
    geom = cast(Place.location, Geometry)
    stop_rows = (
        await session.execute(
            select(Place, ST_X(geom), ST_Y(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    raw_filters = accessibility.get("filters")
    filters = (
        [item for item in raw_filters if isinstance(item, str)]
        if isinstance(raw_filters, list)
        else []
    )
    pace = accessibility.get("travel_pace")
    difficulty = accessibility.get("difficulty_level")
    media = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == route.id,
                    MediaAttachment.status == "active",
                )
                .order_by(MediaAttachment.sort_order)
            )
        ).all()
    )
    return UserRouteEditableOut(
        id=route.id,
        publication_status=route.publication_status,  # type: ignore[arg-type]
        name=route.name,
        description=route.description or "",
        places=[
            UserRouteEditablePlaceOut(
                id=place.id,
                name=place.name,
                subtitle=place.short_description or "",
                lat=lat,
                lng=lng,
            )
            for place, lng, lat in stop_rows
        ],
        filters=filters,
        pace=pace if pace in {"calm", "moderate", "active"} else "calm",
        difficulty=difficulty if isinstance(difficulty, int) and 1 <= difficulty <= 5 else 3,
        media=[
            UserRouteMediaOut(
                id=item.id,
                public_path=item.public_path,
                kind="video" if str(item.content_type or "").startswith("video/") else "image",
                position=item.sort_order,
            )
            for item in media
        ],
        updated_at=route.updated_at,
    )


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
        previous_status = "draft"
    else:
        route = await _owned_editable_route(
            session,
            route_id=payload.route_id,
            owner_user_id=owner_user_id,
        )
        previous_status = route.publication_status
        await session.execute(delete(RouteStop).where(RouteStop.route_id == route.id))

    route.region_id = next(iter(region_ids))
    route.name = payload.name
    route.short_description = payload.description[:240] or None
    route.description = payload.description or None
    route.visibility = "private"
    route.lifecycle_status = "draft"
    # A route that had already been through review goes back into the queue
    # rather than silently to "draft": the author edited something live, and
    # it must not reappear in the catalogue until it is checked again.
    route.publication_status = (
        "pending_review" if previous_status in {"pending_review", "published"} else "draft"
    )
    route.difficulty = _difficulty_name(payload.difficulty)
    route.transport_mode = "walking"
    route.suitable_for_children = "С детьми" in payload.filters
    accessibility: dict[str, Any] = {
        "travel_pace": payload.pace,
        "filters": payload.filters,
        "difficulty_level": payload.difficulty,
    }
    # Road geometry is computed once, here, and read from the database ever
    # after: opening a route redraws it without spending a routing call, and
    # the static map endpoint needs it to draw anything but straight lines.
    # Recomputed on every save because that is exactly when the points can
    # have changed.
    routed = await _route_geometry_for_places(session, places=places, place_ids=payload.place_ids)
    if routed is not None:
        geometry_wkt, routing_meta = routed
        route.geometry = WKTElement(geometry_wkt, srid=4326)
        accessibility["routing"] = routing_meta
    route.accessibility = accessibility
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
    # Narrower than editing: a route already queued or already live has
    # nothing to submit — editing a published one re-queues it by itself.
    route = await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
        allowed=frozenset({"draft", "rejected"}),
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


async def withdraw_user_route(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> UserRouteDraftOut:
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_user_id
        or route.source not in {"user_created", "generated"}
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if route.publication_status not in {"pending_review", "published"}:
        raise AppError(
            code="route_not_withdrawable",
            message="Route cannot be withdrawn in its current status",
            status_code=409,
        )
    route.publication_status = "draft"
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


_logger = logging.getLogger(__name__)

_DRAFT_PREVIEW_TTL_SECONDS = 30 * 60
_DRAFT_PREVIEW_KEY = "route-draft-preview:"


async def _draft_preview_places(
    session: AsyncSession,
    *,
    place_ids: Sequence[UUID],
) -> list[Place]:
    """Places in the order the author placed them, validated like a save.

    The same rules as ``save_user_route_draft``: a preview must never reveal
    an unpublished place, and mixing regions is refused there too.
    """
    rows = list(
        (
            await session.scalars(
                select(Place).where(
                    Place.id.in_(set(place_ids)),
                    Place.publication_status == "published",
                )
            )
        ).all()
    )
    by_id = {place.id: place for place in rows}
    if len(by_id) != len(set(place_ids)):
        raise AppError(
            code="invalid_route_place",
            message="One or more route places are unavailable",
            status_code=400,
        )
    if len({place.region_id for place in rows}) != 1:
        raise AppError(
            code="invalid_route_region",
            message="All route places must belong to one region",
            status_code=400,
        )
    return [by_id[place_id] for place_id in place_ids]


async def _route_geometry_for_places(
    session: AsyncSession,
    *,
    places: list[Place],
    place_ids: Sequence[UUID],
) -> tuple[str, dict[str, Any]] | None:
    """Road line and its provenance for an ordered list of places.

    Returns None when the route cannot be drawn at all (no coordinates), so
    the caller keeps whatever it had rather than storing an empty line.
    A provider outage still returns straight segments, marked synthetic —
    the map is then honest about what it is showing.
    """
    by_id = {place.id: place for place in places}
    ordered = [by_id[place_id] for place_id in place_ids if place_id in by_id]
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom)).where(
                Place.id.in_({place.id for place in ordered})
            )
        )
    ).all()
    point_by_id = {
        place_id: (float(lng), float(lat))
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    }
    waypoints = [
        RouteWaypoint(
            lng=point_by_id[place.id][0],
            lat=point_by_id[place.id][1],
            place_id=place.id,
            label=place.name,
        )
        for place in ordered
        if place.id in point_by_id
    ]
    if len(waypoints) < 2:
        return None

    settings = get_settings()
    try:
        routing = await get_routing_provider(settings).route(
            waypoints=waypoints,
            transport_mode="walk",
        )
    except RoutingError:
        _logger.warning("route_draft_routing_failed", exc_info=True)
        routing = await StubRoutingProvider().route(
            waypoints=waypoints,
            transport_mode="walk",
        )

    geometry_wkt = routing.geometry_wkt
    if not geometry_wkt:
        points = ", ".join(f"{point.lng:.6f} {point.lat:.6f}" for point in waypoints)
        geometry_wkt = f"LINESTRING({points})"
    return geometry_wkt, {
        "provider": routing.provider,
        "synthetic": routing.synthetic,
        "distance_meters": routing.total_distance_meters,
        "movement_duration_seconds": routing.total_duration_seconds,
        "warnings": list(routing.warnings),
        "road_types": list(routing.road_types),
        "quality_status": "unverified",
    }


async def preview_user_route_draft(
    session: AsyncSession,
    *,
    payload: RouteDraftPreviewIn,
    redis: Redis | None = None,
) -> RouteDraftPreviewOut:
    """Road geometry for draft points, before there is a route to save.

    The publish form used to draw a stylised placeholder because the static
    map endpoint needs a saved route id. Authors place points and see a
    diagram instead of the roads they will actually walk.

    Routing failures degrade to straight segments rather than an error: a
    preview is advisory, and a straight line on the real basemap still tells
    the author more than the placeholder did.
    """
    places = await _draft_preview_places(session, place_ids=payload.place_ids)
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom)).where(
                Place.id.in_({place.id for place in places})
            )
        )
    ).all()
    point_by_id = {
        place_id: (float(lng), float(lat))
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    }
    waypoints = [
        RouteWaypoint(
            lng=point_by_id[place.id][0],
            lat=point_by_id[place.id][1],
            place_id=place.id,
            label=place.name,
        )
        for place in places
        if place.id in point_by_id
    ]
    if len(waypoints) < 2:
        raise AppError(
            code="invalid_route_place",
            message="Route points have no coordinates",
            status_code=400,
        )

    settings = get_settings()
    transport_mode = type_cast(TransportMode, payload.transport_mode)
    try:
        routing = await get_routing_provider(settings).route(
            waypoints=waypoints,
            transport_mode=transport_mode,
        )
    except RoutingError:
        _logger.warning("route_draft_preview_routing_failed", exc_info=True)
        routing = await StubRoutingProvider().route(
            waypoints=waypoints,
            transport_mode=transport_mode,
        )

    geometry: RouteGeometryOut | None = None
    if routing.geometry_wkt:
        raw = await session.scalar(
            select(func.ST_AsGeoJSON(func.ST_GeomFromText(routing.geometry_wkt, 4326)))
        )
        if raw:
            geometry = RouteGeometryOut.model_validate(json.loads(raw))
    stops = [(point.lng, point.lat) for point in waypoints]
    line = list(geometry.coordinates) if geometry else stops

    preview_id = uuid4().hex
    if redis is not None:
        try:
            await redis.set(
                f"{_DRAFT_PREVIEW_KEY}{preview_id}",
                json.dumps({"line": line, "stops": stops}),
                ex=_DRAFT_PREVIEW_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001 — the raster falls back to the points
            _logger.warning("route_draft_preview_cache_failed", exc_info=True)

    return RouteDraftPreviewOut(
        preview_id=preview_id,
        geometry=geometry,
        distance_meters=routing.total_distance_meters,
        duration_seconds=routing.total_duration_seconds,
        provider=routing.provider,
        synthetic=routing.synthetic,
    )


async def draft_preview_shape(
    redis: Redis | None,
    preview_id: str,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """Cached ``(line, stops)`` for a preview, or None once it has expired."""
    if redis is None:
        return None
    try:
        raw = await redis.get(f"{_DRAFT_PREVIEW_KEY}{preview_id}")
    except Exception:  # noqa: BLE001 — a cache outage is a 404, not a 500
        _logger.warning("route_draft_preview_read_failed", exc_info=True)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        line = [(float(x), float(y)) for x, y in data["line"]]
        stops = [(float(x), float(y)) for x, y in data["stops"]]
    except Exception:  # noqa: BLE001 — a corrupt entry behaves like a miss
        _logger.warning("route_draft_preview_decode_failed", exc_info=True)
        return None
    return (line, stops) if len(line) >= 2 else None
