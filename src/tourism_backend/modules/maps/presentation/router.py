from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from uuid import UUID

import httpx
from fastapi import APIRouter, Query, Request, Response

from tourism_backend.api.deps import DbSession, SettingsDep
from tourism_backend.api.errors import AppError
from tourism_backend.modules.places.application import service as places_service
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    two_gis_routing_stats,
)
from tourism_backend.modules.routes.application import service as routes_service

router = APIRouter(tags=["maps"])
_STATIC_URL = "https://static.maps.2gis.com/2.0"
_CACHE_TTL_SECONDS = 86_400
_CACHE_MAX_ITEMS = 128
_cache: dict[tuple[tuple[str, str], ...], tuple[float, bytes, str]] = {}


def _size(width: int, height: int, scale: int) -> str:
    return f"{width}x{height}@{scale}x"


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
    params: list[tuple[str, str]] = [
        ("s", _size(width, height, scale)),
        ("ls", _line(line_points) + "~c:16a34a~w:5"),
    ]
    # An explicit center+zoom makes the projection deterministic, so a client
    # can place its own tappable pins over the raster (2GIS static maps use
    # standard 256px Web Mercator — verified against known pixel offsets).
    # Without it the provider auto-fits and the exact viewport is unknown.
    if center is not None and zoom is not None:
        params.append(("c", f"{center[1]:.6f},{center[0]:.6f}"))
        params.append(("z", str(zoom)))
    if pins == "numbered":
        # pt marker color only accepts 2GIS's predefined short codes (be/rd/
        # oe/yw/gn/pe/pk/gy/bk), unlike ls which takes an arbitrary hex
        # RRGGBB. Markers must sit on the real stops, not on points sampled
        # from the road-following geometry (which follows the road).
        for index, (lon, lat) in enumerate(stop_points[:8], start=1):
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


@router.get("/maps/static/route/{route_id}")
async def route_static_map(
    route_id: UUID,
    session: DbSession,
    settings: SettingsDep,
    request: Request,
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
    params = _route_static_params(
        line_points,
        stop_points or line_points,
        width=width,
        height=height,
        scale=scale,
        center=(center_lat, center_lng) if has_center else None,  # type: ignore[arg-type]
        zoom=zoom if has_center else None,
        pins=pins,
    )
    return await _fetch(settings=settings, params=params, request=request)


@router.get("/maps/static/place/{place_id}")
async def place_static_map(
    place_id: UUID,
    session: DbSession,
    settings: SettingsDep,
    request: Request,
    width: int = Query(default=880, ge=120, le=1280),
    height: int = Query(default=420, ge=90, le=1280),
    scale: int = Query(default=2, ge=1, le=2),
) -> Response:
    place = await places_service.get_place(session, place_id)
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
