from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from uuid import UUID

import httpx
from fastapi import APIRouter, Query, Request, Response

from tourism_backend.api.deps import DbSession, SettingsDep
from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.maps.infrastructure.osm_static import (
    MapFrame,
    StaticMapError,
    draw_overlays,
    fetch_basemap,
    fit_frame,
)
from tourism_backend.modules.places.application import service as places_service
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    two_gis_routing_stats,
)
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.structure_rules import segment_mode_for

router = APIRouter(tags=["maps"])
_STATIC_URL = "https://static.maps.2gis.com/2.0"
_CACHE_TTL_SECONDS = 86_400
_CACHE_MAX_ITEMS = 128
_cache: dict[tuple[tuple[str, str], ...], tuple[float, bytes, str]] = {}


def _size(width: int, height: int, scale: int) -> str:
    return f"{width}x{height}@{scale}x"


# A road-following polyline can carry thousands of vertices (verified: a
# 48km route had 2092). Encoded at full precision that blew past 2GIS static
# maps' URL length limit — the provider returned 414 and every request for
# that route fell back to the app's plain pins-only preview. A static
# preview at a few hundred pixels doesn't need meter-level fidelity, so the
# line is decimated before encoding; empirically confirmed OK at 800 points,
# this keeps real margin under that.
_MAX_LINE_POINTS = 400


def _downsample(
    points: Sequence[tuple[float, float]], max_points: int
) -> Sequence[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    last = len(points) - 1
    # Evenly spaced indices spanning the full route, both endpoints included
    # by construction; dedup only ever removes near-duplicates from rounding,
    # so the result never exceeds max_points.
    indices = {round(i * last / (max_points - 1)) for i in range(max_points)}
    return [points[i] for i in sorted(indices)]


# 2GIS static maps reject any object outside the requested frame with 400
# "object is out of bounds". A frame zoomed in on one leg therefore can never
# carry the whole route line: it is clipped to the frame first. The inset
# keeps rounding of the 6-decimal coordinates from landing a hair outside.
_FRAME_INSET_PX = 2.0
_TILE_SIZE = 256.0
_MAX_LINE_PIECES = 16


def _world_xy(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    world = _TILE_SIZE * 2**zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    x = (lon + 180.0) / 360.0 * world
    y = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * world
    return x, y


def _lon_lat(x: float, y: float, zoom: int) -> tuple[float, float]:
    world = _TILE_SIZE * 2**zoom
    lon = x / world * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / world))))
    return lon, lat


def _frame_box(
    center: tuple[float, float], zoom: int, width: int, height: int
) -> tuple[float, float, float, float]:
    """Pixel bounds of the frame in world coordinates (`s` is logical px)."""
    cx, cy = _world_xy(center[1], center[0], zoom)
    half_w = width / 2 - _FRAME_INSET_PX
    half_h = height / 2 - _FRAME_INSET_PX
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h


def _clip_segment(
    a: tuple[float, float], b: tuple[float, float], box: tuple[float, float, float, float]
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang-Barsky: the part of segment a-b inside box, or None."""
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x0 - box[0]),
        (dx, box[2] - x0),
        (-dy, y0 - box[1]),
        (dy, box[3] - y0),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def _clip_line(
    points: Sequence[tuple[float, float]],
    box: tuple[float, float, float, float],
    zoom: int,
) -> list[list[tuple[float, float]]]:
    """Pieces of a lon,lat polyline that lie inside the frame, in lon,lat."""
    pixels = [_world_xy(lon, lat, zoom) for lon, lat in points]
    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for a, b in zip(pixels, pixels[1:], strict=False):
        clipped = _clip_segment(a, b, box)
        if clipped is None:
            if len(current) >= 2:
                pieces.append(current)
            current = []
            continue
        start, end = clipped
        if not current or current[-1] != start:
            if len(current) >= 2:
                pieces.append(current)
            current = [start]
        current.append(end)
        if end != b:
            # The segment leaves the frame here.
            pieces.append(current)
            current = []
    if len(current) >= 2:
        pieces.append(current)
    # The longest stretches matter; the URL has room for a bounded number.
    pieces.sort(key=len, reverse=True)
    return [[_lon_lat(x, y, zoom) for x, y in piece] for piece in pieces[:_MAX_LINE_PIECES]]


def _inside(point: tuple[float, float], box: tuple[float, float, float, float], zoom: int) -> bool:
    x, y = _world_xy(point[0], point[1], zoom)
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _line(points: Sequence[tuple[float, float]]) -> str:
    # Static API expects latitude,longitude (the backend geometry is lon,lat).
    return ",".join(f"{lat:.6f},{lon:.6f}" for lon, lat in points)


def _route_static_params(
    line_points: Sequence[tuple[float, float]],
    stop_points: Sequence[tuple[float, float]],
    *,
    width: int,
    height: int,
    scale: int,
    center: tuple[float, float] | None = None,
    zoom: int | None = None,
    pins: str = "numbered",
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("s", _size(width, height, scale))]
    # An explicit center+zoom makes the projection deterministic, so a client
    # can place its own tappable pins over the raster (2GIS static maps use
    # standard 256px Web Mercator — verified against known pixel offsets).
    # Without it the provider auto-fits and the exact viewport is unknown.
    box = None
    if center is not None and zoom is not None:
        box = _frame_box(center, zoom, width, height)
        pieces = _clip_line(line_points, box, zoom)
        total = sum(len(piece) for piece in pieces)
        for piece in pieces:
            budget = max(2, _MAX_LINE_POINTS * len(piece) // max(total, 1))
            params.append(("ls", _line(_downsample(piece, budget)) + "~c:16a34a~w:5"))
        # Static API takes latitude first, same as the `pt`/`ls` values above.
        params.append(("c", f"{center[0]:.6f},{center[1]:.6f}"))
        params.append(("z", str(zoom)))
    else:
        params.insert(
            1, ("ls", _line(_downsample(line_points, _MAX_LINE_POINTS)) + "~c:16a34a~w:5")
        )
    if pins == "numbered":
        # pt marker color only accepts 2GIS's predefined short codes (be/rd/
        # oe/yw/gn/pe/pk/gy/bk), unlike ls which takes an arbitrary hex
        # RRGGBB. Markers must sit on the real stops, not on points sampled
        # from the road-following geometry (which follows the road).
        for index, (lon, lat) in enumerate(stop_points[:8], start=1):
            if box is not None and zoom is not None and not _inside((lon, lat), box, zoom):
                continue
            params.append(("pt", f"{lat:.6f},{lon:.6f}~k:c~c:gn~n:{index}"))
    return params


async def _fetch(
    *,
    settings: SettingsDep,
    params: list[tuple[str, str]],
    request: Request,
) -> Response:
    key = settings.two_gis_http_api_key
    if key is None or not key.get_secret_value().strip():
        raise AppError(
            code="map_preview_unavailable",
            message="Map preview is not configured",
            status_code=503,
        )
    cache_key = tuple(params)
    cached = _cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        content, etag = cached[1], cached[2]
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            content=content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400", "ETag": etag},
        )
    params.append(("key", key.get_secret_value()))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.get(_STATIC_URL, params=tuple(params))
    except httpx.HTTPError as exc:
        raise AppError(
            code="map_preview_upstream_unavailable",
            message="Map provider is temporarily unavailable",
            status_code=502,
        ) from exc
    if upstream.status_code == 429:
        raise AppError(
            code="map_preview_rate_limited",
            message="Map provider rate limit reached",
            status_code=429,
        )
    if upstream.status_code >= 400 or not upstream.content:
        raise AppError(
            code="map_preview_upstream_error",
            message="Map preview could not be generated",
            status_code=502,
        )
    digest = hashlib.sha256(upstream.content).hexdigest()
    etag = f'"{digest}"'
    if len(_cache) >= _CACHE_MAX_ITEMS:
        _cache.pop(next(iter(_cache)))
    _cache[cache_key] = (now, upstream.content, etag)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=upstream.content,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        },
    )


_OSM_TIMEOUT_SECONDS = 10.0
_osm_cache: dict[tuple[object, ...], tuple[float, bytes, str]] = {}


def _png_response(content: bytes, etag: str, request: Request, cache_control: str) -> Response:
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": cache_control,
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _osm_image(
    *,
    settings: Settings,
    request: Request,
    frame: MapFrame,
    line: Sequence[tuple[float, float]] = (),
    numbered_pins: Sequence[tuple[float, float]] = (),
    place_pin: tuple[float, float] | None = None,
    line_mode: str = "walk",
    pieces: Sequence[tuple[str, Sequence[tuple[float, float]]]] = (),
) -> Response:
    line_digest = hashlib.sha256(
        repr(
            (
                tuple(line),
                tuple(numbered_pins),
                line_mode,
                tuple((mode, tuple(points)) for mode, points in pieces),
            )
        ).encode()
    ).hexdigest()
    key = (settings.map_source_version, frame, line_digest, place_pin)
    now = time.monotonic()
    cached = _osm_cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return _png_response(cached[1], cached[2], request, "public, max-age=86400")
    try:
        base = await fetch_basemap(
            frame, base_url=settings.tileserver_base_url, timeout_seconds=_OSM_TIMEOUT_SECONDS
        )
    except StaticMapError as exc:
        raise AppError(
            code="map_preview_upstream_unavailable",
            message="Map renderer is temporarily unavailable",
            status_code=502,
        ) from exc
    content = draw_overlays(
        base,
        frame,
        line=line,
        numbered_pins=numbered_pins,
        place_pin=place_pin,
        line_mode=line_mode,
        pieces=pieces,
    )
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    if len(_osm_cache) >= _CACHE_MAX_ITEMS:
        _osm_cache.pop(next(iter(_osm_cache)))
    _osm_cache[key] = (now, content, etag)
    return _png_response(
        content, etag, request, "public, max-age=86400, stale-while-revalidate=604800"
    )


async def route_map_response(
    *,
    settings: Settings,
    request: Request,
    line: Sequence[tuple[float, float]],
    stops: Sequence[tuple[float, float]],
    width: int,
    height: int,
    scale: int,
    center: tuple[float, float] | None,
    zoom: int | None,
    pins: str,
    line_mode: str = "walk",
    pieces: Sequence[tuple[str, Sequence[tuple[float, float]]]] = (),
) -> Response:
    """One route image for every endpoint that shows a route (spec 12a).

    ``line_mode`` is a segment mode (spec 14): walking is drawn dashed.
    """
    if settings.map_provider == "2gis":
        return await _fetch(
            settings=settings,
            request=request,
            params=_route_static_params(
                line,
                stops,
                width=width,
                height=height,
                scale=scale,
                center=center,
                zoom=zoom,
                pins=pins,
            ),
        )
    if center is not None and zoom is not None:
        frame = MapFrame(center[0], center[1], zoom, width, height, scale)
    else:
        everything = list(line) + [p for _, points in pieces for p in points]
        frame = fit_frame(everything + list(stops), width=width, height=height, scale=scale)
    return await _osm_image(
        settings=settings,
        request=request,
        frame=frame,
        line=line,
        numbered_pins=stops if pins == "numbered" else (),
        line_mode=line_mode,
        pieces=pieces,
    )


@router.get("/maps/static/route/{route_id}")
@router.get("/maps/static/route/{route_id}/{version}")
async def route_static_map(
    route_id: UUID,
    session: DbSession,
    settings: SettingsDep,
    request: Request,
    # The renderer version only busts caches (v=osm1, D22); any value works.
    version: str | None = None,
    width: int = Query(default=880, ge=120, le=1280),
    height: int = Query(default=420, ge=90, le=1280),
    scale: int = Query(default=2, ge=1, le=2),
    center_lat: float | None = Query(default=None, ge=-90, le=90),
    center_lng: float | None = Query(default=None, ge=-180, le=180),
    zoom: int | None = Query(default=None, ge=1, le=18),
    pins: str = Query(default="numbered", pattern="^(numbered|none)$"),
) -> Response:
    route = await routes_service.get_route(session, route_id)
    stop_points = [
        (stop.lng, stop.lat)
        for stop in route.stops
        if stop.lng is not None and stop.lat is not None
    ]
    line_points = route.geometry.coordinates if route.geometry else []
    if len(line_points) < 2:
        line_points = stop_points
    if not line_points:
        raise AppError(
            code="map_preview_unavailable",
            message="Route has no coordinates",
            status_code=404,
        )
    has_center = center_lat is not None and center_lng is not None
    return await route_map_response(
        settings=settings,
        request=request,
        line=line_points,
        stops=stop_points or line_points,
        width=width,
        height=height,
        scale=scale,
        center=(center_lat, center_lng) if has_center else None,  # type: ignore[arg-type]
        zoom=zoom if has_center else None,
        pins=pins,
        line_mode=segment_mode_for(route.transport_mode),
        pieces=await routes_service.route_segment_lines(session, route_id),
    )


@router.get("/maps/static/place/{place_id}")
@router.get("/maps/static/place/{place_id}/{version}")
async def place_static_map(
    place_id: UUID,
    session: DbSession,
    settings: SettingsDep,
    request: Request,
    # The renderer version only busts caches (v=osm1, D22); any value works.
    version: str | None = None,
    width: int = Query(default=880, ge=120, le=1280),
    height: int = Query(default=420, ge=90, le=1280),
    scale: int = Query(default=2, ge=1, le=2),
) -> Response:
    place = await places_service.get_place(session, place_id)
    if settings.map_provider == "osm":
        return await _osm_image(
            settings=settings,
            request=request,
            frame=MapFrame(place.lat, place.lng, 14, width, height, scale),
            place_pin=(place.lng, place.lat),
        )
    params = [
        ("s", _size(width, height, scale)),
        ("c", f"{place.lat:.6f},{place.lng:.6f}"),
        ("z", "14"),
        ("pt", f"{place.lat:.6f},{place.lng:.6f}~k:p~c:rd~s:l"),
    ]
    return await _fetch(settings=settings, params=params, request=request)


@router.get("/maps/two-gis/status")
async def two_gis_status(settings: SettingsDep) -> dict[str, object]:
    """Ops-safe status: configured/provider/circuit + counters. No secrets."""
    key = settings.two_gis_http_api_key
    return {
        "routing_provider": settings.routing_provider,
        "configured": key is not None and bool(key.get_secret_value().strip()),
        **two_gis_routing_stats(),
    }
