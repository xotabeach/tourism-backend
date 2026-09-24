"""Route one route again and keep what the router gave (spec 12a/14b).

Shared by the editors' page in the admin and scripts/rebuild_routes_osm.py:
a route's line, length, legs, segments and days all come from here, with
the car parks and day boundaries editors set kept by place.
"""

from __future__ import annotations

from typing import Any

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import get_settings
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    routing_details,
)
from tourism_backend.modules.route_builder.infrastructure.routing_factory import (
    get_routing_provider,
)
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.application.structure_rules import segment_mode_for
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop


def _parking(route: Route, place_id: Any) -> tuple[float, float] | None:
    raw = (route.parking_overrides or {}).get(str(place_id))
    if (
        isinstance(raw, list)
        and len(raw) == 2
        and all(isinstance(value, (int, float)) for value in raw)
    ):
        return float(raw[0]), float(raw[1])
    return None


async def route_waypoints(session: AsyncSession, route: Route) -> list[RouteWaypoint]:
    """The route's stops in order, with the car parks editors chose."""
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, Place.name, ST_X(geom), ST_Y(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    return [
        RouteWaypoint(
            lng=float(lng),
            lat=float(lat),
            place_id=place_id,
            label=name,
            parking=_parking(route, place_id),
        )
        for place_id, name, lng, lat in rows
        if lng is not None and lat is not None
    ]


def _straight(waypoints: list[RouteWaypoint]) -> str:
    return "LINESTRING(" + ", ".join(f"{p.lng:.6f} {p.lat:.6f}" for p in waypoints) + ")"


def store_routing(
    route: Route,
    waypoints: list[RouteWaypoint],
    result: RoutingResult | None,
    data_version: str | None,
) -> None:
    """Replace the route's line and routing facts with this answer.

    No answer keeps a straight line marked «не проверено» (spec 12a, D14).
    """
    meta = dict((route.accessibility or {}).get("routing") or {})
    for stale in ("legs", "segments", "steep_segment", "backfilled", "road_types"):
        meta.pop(stale, None)
    if result is None or not result.geometry_wkt:
        route.geometry = WKTElement(_straight(waypoints), srid=4326)
        meta.update(
            {
                "provider": None,
                "synthetic": True,
                "geometry_available": False,
                "quality_status": "unverified",
                "provider_version": data_version,
            }
        )
    else:
        route.geometry = WKTElement(result.geometry_wkt, srid=4326)
        route.distance_meters = result.total_distance_meters
        meta.update(
            {
                "provider": result.provider,
                "synthetic": False,
                "geometry_available": True,
                "distance_meters": result.total_distance_meters,
                "movement_duration_seconds": result.total_duration_seconds,
                "road_types": list(result.road_types),
                **routing_details(result, stop_count=len(waypoints), data_version=data_version),
            }
        )
    accessibility = dict(route.accessibility or {})
    accessibility["routing"] = meta
    route.accessibility = accessibility


async def reroute_route(session: AsyncSession, route: Route) -> RoutingResult | None:
    """Route the stops again, store the answer and rebuild segments and days."""
    settings = get_settings()
    waypoints = await route_waypoints(session, route)
    if len(waypoints) < 2:
        return None
    try:
        result: RoutingResult | None = await get_routing_provider(settings).route(
            waypoints=waypoints, transport_mode=segment_mode_for(route.transport_mode)
        )
    except RoutingError:
        result = None
    store_routing(route, waypoints, result, settings.osm_data_version)
    await refresh_route_structure(session, route)
    return result
