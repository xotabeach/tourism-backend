"""The page of one route: stops, days, segments, geometry and routing facts."""

import json
import math
from typing import Any
from typing import cast as type_cast
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y, ST_AsGeoJSON
from sqlalchemy import (
    cast,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application import place_covers
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.application.route_catalog import (
    _PUBLIC_CATALOG,
    _author_fields_for_routes,
    _cover_urls_for_routes,
    _has_unpublished_stop,
    _to_list_item,
    route_ratings,
    with_revision_state,
)
from tourism_backend.modules.routes.application.route_revisions import revision_of
from tourism_backend.modules.routes.application.schemas import (
    RouteDayOut,
    RouteDetailOut,
    RouteGeometryOut,
    RouteMediaOut,
    RouteQualityStatus,
    RouteRoutingOut,
    RouteSegmentOut,
    RouteStopOut,
)
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)


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
        accessibility=_without_segment_shapes(route.accessibility),
        freshness_status=route.freshness_status,
        geometry=geometry,
        routing=routing,
        stops=stops,
        media=media,
        static_map_url=f"/api/v1/maps/static/route/{route.id}/{get_settings().map_source_version}",
        base_mode=route.base_mode,
        needs_public_transport=route.needs_public_transport,
        segments=await _segments_for_route(session, route.id),
        days=await _days_for_route(session, route.id),
    )


async def _days_for_route(session: AsyncSession, route_id: UUID) -> list[RouteDayOut]:
    return [
        RouteDayOut(
            day_index=day.day_index,
            first_stop_id=day.first_stop_id,
            last_stop_id=day.last_stop_id,
            boundary_source=day.boundary_source,
            overnight_note=day.overnight_note,
            overloaded=day.overloaded,
            difficulty_level=day.difficulty_level,
        )
        for day in await session.scalars(
            select(RouteDay).where(RouteDay.route_id == route_id).order_by(RouteDay.day_index)
        )
    ]


def _without_segment_shapes(accessibility: dict[str, Any] | None) -> dict[str, Any] | None:
    """The encoded segment lines are served as ``segments``; not twice."""
    if not isinstance(accessibility, dict):
        return accessibility
    routing = accessibility.get("routing")
    if not isinstance(routing, dict) or "segments" not in routing:
        return accessibility
    return {**accessibility, "routing": {k: v for k, v in routing.items() if k != "segments"}}


async def _segments_for_route(session: AsyncSession, route_id: UUID) -> list[RouteSegmentOut]:
    rows = (
        await session.execute(
            select(RouteSegment, ST_AsGeoJSON(RouteSegment.geometry))
            .where(RouteSegment.route_id == route_id)
            .order_by(RouteSegment.leg_index, RouteSegment.seq)
        )
    ).all()
    segments: list[RouteSegmentOut] = []
    for segment, raw in rows:
        geometry = None
        if raw:
            coordinates = json.loads(raw).get("coordinates") or []
            geometry = RouteGeometryOut(
                coordinates=[(float(x), float(y)) for x, y, *_ in coordinates]
            )
        segments.append(
            RouteSegmentOut(
                leg_index=segment.leg_index,
                seq=segment.seq,
                mode=segment.mode,
                role=segment.role,
                origin=segment.origin,
                distance_meters=segment.distance_meters,
                duration_seconds=segment.duration_seconds,
                elevation_gain_meters=segment.elevation_gain_meters,
                geometry=geometry,
            )
        )
    return segments


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
            Route.revision_of_route_id.is_(None),
        )
    )
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    detail = await _route_detail_from_model(session, route, public_stops_only=False)
    return with_revision_state(detail, await revision_of(session, route.id))
