"""Preview of a draft and the cached road lines behind it."""

import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y, ST_AsGeoJSON
from redis.asyncio import Redis
from sqlalchemy import (
    cast,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
    routing_details,
)
from tourism_backend.modules.route_builder.infrastructure.routing_factory import (
    get_routing_provider,
)
from tourism_backend.modules.route_builder.infrastructure.routing_stub import (
    StubRoutingProvider,
)
from tourism_backend.modules.routes.application.difficulty import (
    quick_estimate,
)
from tourism_backend.modules.routes.application.schemas import (
    RouteDraftPreviewIn,
    RouteDraftPreviewOut,
    RouteGeometryOut,
)
from tourism_backend.modules.routes.application.structure_rules import segment_mode_for
from tourism_backend.modules.routes.infrastructure.models import (
    RouteSegment,
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
    redis: Redis | None = None,
    transport_mode: TransportMode = "walk",
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
    return await routing_line_for_waypoints(waypoints, redis=redis, transport_mode=transport_mode)


_ROUTING_CACHE_KEY = "route-routing:"


# The author's tag for a driven route (route_publish tags in the app).
CAR_TAG = "На машине"


_ROUTING_CACHE_TTL_SECONDS = 24 * 60 * 60


def routing_fingerprint(
    waypoints: Sequence[RouteWaypoint],
    *,
    transport_mode: str,
) -> str:
    """Stable id for "this road line, through these points, on foot".

    Coordinates are rounded to ~10cm before hashing: the same place always
    produces the same key, and a float that differs in its last bit does not
    silently spend a routing call.
    """
    payload = "|".join(f"{point.lng:.6f},{point.lat:.6f}" for point in waypoints)
    digest = hashlib.sha256(f"{transport_mode}:{payload}".encode()).hexdigest()
    return digest[:32]


async def cached_routing_line(
    redis: Redis | None,
    fingerprint: str,
) -> tuple[str, dict[str, Any]] | None:
    """A previously computed line for [fingerprint], if it is still cached.

    Saving a draft used to route again from scratch even though the form had
    just previewed the very same points seconds earlier — the author waited
    on a second external call for an answer already known (reported
    2026-09-08 as "очень долгий запрос на сохранение черновика").
    """
    if redis is None:
        return None
    try:
        raw = await redis.get(f"{_ROUTING_CACHE_KEY}{fingerprint}")
    except Exception:  # noqa: BLE001 — a cache outage just means routing again
        _logger.warning("route_routing_cache_read_failed", exc_info=True)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return str(data["wkt"]), dict(data["meta"])
    except Exception:  # noqa: BLE001 — a corrupt entry behaves like a miss
        _logger.warning("route_routing_cache_decode_failed", exc_info=True)
        return None


async def store_routing_line(
    redis: Redis | None,
    fingerprint: str,
    *,
    geometry_wkt: str,
    meta: dict[str, Any],
) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            f"{_ROUTING_CACHE_KEY}{fingerprint}",
            json.dumps({"wkt": geometry_wkt, "meta": meta}),
            ex=_ROUTING_CACHE_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 — caching is an optimisation, never a gate
        _logger.warning("route_routing_cache_write_failed", exc_info=True)


async def routing_line_for_waypoints(
    waypoints: list[RouteWaypoint],
    *,
    redis: Redis | None = None,
    transport_mode: TransportMode = "walk",
) -> tuple[str, dict[str, Any]]:
    """Road line through [waypoints], with a plain line as the last resort.

    Never raises: saving a draft must not depend on a router being willing
    to route it. Consults the shared routing cache first, so a save that
    follows a preview of the same points costs nothing.
    """
    fingerprint = routing_fingerprint(waypoints, transport_mode=transport_mode)
    cached = await cached_routing_line(redis, fingerprint)
    if cached is not None:
        return cached
    settings = get_settings()
    routing: RoutingResult | None
    try:
        routing = await get_routing_provider(settings).route(
            waypoints=waypoints,
            transport_mode=transport_mode,
        )
    except RoutingError:
        _logger.warning("route_draft_routing_failed", exc_info=True)
        try:
            routing = await StubRoutingProvider().route(
                waypoints=waypoints,
                transport_mode=transport_mode,
            )
        except RoutingError:
            # The stub refuses the same things the provider does — a walking
            # leg over its 25km ceiling, say. Worth warning an author about,
            # but not a reason to refuse to save their draft, so the route
            # keeps a plain line through its points.
            _logger.warning("route_draft_routing_unavailable", exc_info=True)
            routing = None

    straight = ", ".join(f"{point.lng:.6f} {point.lat:.6f}" for point in waypoints)
    if routing is None:
        line = f"LINESTRING({straight})"
        meta: dict[str, Any] = {
            "provider": None,
            "synthetic": True,
            "quality_status": "unverified",
        }
    else:
        line = routing.geometry_wkt or f"LINESTRING({straight})"
        meta = {
            "provider": routing.provider,
            "synthetic": routing.synthetic,
            "distance_meters": routing.total_distance_meters,
            "movement_duration_seconds": routing.total_duration_seconds,
            "warnings": list(routing.warnings),
            "road_types": list(routing.road_types),
            "quality_status": "unverified",
            **routing_details(
                routing, stop_count=len(waypoints), data_version=settings.osm_data_version
            ),
        }
    await store_routing_line(redis, fingerprint, geometry_wkt=line, meta=meta)
    return line, meta


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
    # Old spellings (bicycle, public_transport) are routed as walked or
    # driven: nothing else is routed before 12b (spec 14, R4).
    transport_mode: TransportMode = segment_mode_for(payload.transport_mode)
    # The form previews the same points repeatedly while the author drags one
    # around, and then saves them. All of that is one routing answer.
    fingerprint = routing_fingerprint(waypoints, transport_mode=transport_mode)
    cached = await cached_routing_line(redis, fingerprint)
    geometry_wkt: str | None
    meta: dict[str, Any]
    if cached is not None:
        geometry_wkt, meta = cached
    else:
        try:
            routing = await get_routing_provider(settings).route(
                waypoints=waypoints,
                transport_mode=transport_mode,
            )
        except RoutingError:
            _logger.warning("route_draft_preview_routing_failed", exc_info=True)
            try:
                routing = await StubRoutingProvider().route(
                    waypoints=waypoints,
                    transport_mode=transport_mode,
                )
            except RoutingError:
                # Before D24 a walk leg over 25 km was refused here as well,
                # and the preview answered 500 (FRONTEND-44): never again.
                _logger.warning("route_draft_preview_unavailable", exc_info=True)
                routing = None
        straight = ", ".join(f"{point.lng:.6f} {point.lat:.6f}" for point in waypoints)
        geometry_wkt = (routing.geometry_wkt if routing else None) or f"LINESTRING({straight})"
        meta = (
            {"provider": None, "synthetic": True, "quality_status": "unverified"}
            if routing is None
            else {
                "provider": routing.provider,
                "synthetic": routing.synthetic,
                "distance_meters": routing.total_distance_meters,
                "movement_duration_seconds": routing.total_duration_seconds,
                "warnings": list(routing.warnings),
                "road_types": list(routing.road_types),
                "quality_status": "unverified",
                **routing_details(
                    routing, stop_count=len(waypoints), data_version=settings.osm_data_version
                ),
            }
        )
        await store_routing_line(
            redis,
            fingerprint,
            geometry_wkt=geometry_wkt,
            meta=meta,
        )

    geometry: RouteGeometryOut | None = None
    if geometry_wkt:
        raw = await session.scalar(
            select(func.ST_AsGeoJSON(func.ST_GeomFromText(geometry_wkt, 4326)))
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
                json.dumps({"line": line, "stops": stops, "mode": transport_mode}),
                ex=_DRAFT_PREVIEW_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001 — the raster falls back to the points
            _logger.warning("route_draft_preview_cache_failed", exc_info=True)

    return RouteDraftPreviewOut(
        preview_id=preview_id,
        geometry=geometry,
        distance_meters=int(meta.get("distance_meters") or 0),
        duration_seconds=int(meta.get("movement_duration_seconds") or 0),
        # A cached line from a provider outage carries no provider name; the
        # `synthetic` flag next to it is what tells the client not to present
        # it as roads.
        provider=str(meta.get("provider") or "none"),
        synthetic=bool(meta.get("synthetic")),
        difficulty_level=quick_estimate(
            mode=transport_mode,
            distance_meters=_int_value(meta.get("distance_meters")),
            duration_seconds=_int_value(meta.get("movement_duration_seconds")),
            elevation_gain_meters=_int_value(meta.get("elevation_gain_meters")),
            elevation_loss_meters=_int_value(meta.get("elevation_loss_meters")),
            segments=[item for item in meta.get("segments") or [] if isinstance(item, dict)],
            synthetic=bool(meta.get("synthetic")),
        ),
    )


def _int_value(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


async def draft_preview_shape(
    redis: Redis | None,
    preview_id: str,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], str] | None:
    """Cached ``(line, stops, mode)`` for a preview, or None once it has expired."""
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
        mode = segment_mode_for(data.get("mode"))
    except Exception:  # noqa: BLE001 — a corrupt entry behaves like a miss
        _logger.warning("route_draft_preview_decode_failed", exc_info=True)
        return None
    return (line, stops, mode) if len(line) >= 2 else None


def retraces_approach(role: str, leg_index: int, approach_legs: set[int]) -> bool:
    """Whether a segment only walks back along the approach to its stop.

    The walk back to the car leaves the stop the previous leg walked up to,
    along the same path. The first stop has no previous leg, and a stop the
    car reached has no approach: there the walk back is the only line that
    touches the stop (BACKEND-63).
    """
    return role == "return" and (leg_index - 1) in approach_legs


async def route_segment_lines(
    session: AsyncSession, route_id: UUID
) -> list[tuple[str, list[tuple[float, float]]]]:
    """(mode, line) of each segment, in order, when every segment has a line.

    Only then can the map draw the drive and the walks apart (spec 14, D23);
    otherwise it draws the route line in the route's own mode. A walk back
    to the car that retraces the approach is left out: drawing both would
    fill the dashes in.
    """
    rows = (
        await session.execute(
            select(
                RouteSegment.mode,
                RouteSegment.role,
                RouteSegment.leg_index,
                ST_AsGeoJSON(RouteSegment.geometry),
            )
            .where(RouteSegment.route_id == route_id)
            .order_by(RouteSegment.leg_index, RouteSegment.seq)
        )
    ).all()
    approach_legs = {leg_index for _mode, role, leg_index, _raw in rows if role == "approach"}
    pieces: list[tuple[str, list[tuple[float, float]]]] = []
    for mode, role, leg_index, raw in rows:
        if retraces_approach(role, leg_index, approach_legs):
            continue
        if not raw:
            return []
        coordinates = json.loads(raw).get("coordinates") or []
        pieces.append((mode, [(float(x), float(y)) for x, y, *_ in coordinates]))
    return pieces
