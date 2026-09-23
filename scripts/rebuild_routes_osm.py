#!/usr/bin/env python3
"""Rebuild every route on our OSM router and clear 2GIS data (spec 12a, section 6).

For each active route with two or more located stops the configured router
(Valhalla) builds a fresh line, length, per-leg data, elevation and the data
version, which replace what the route kept (D14). A route Valhalla cannot
build loses any 2GIS line it had and keeps a straight line marked
«не проверено»: 2GIS terms forbid keeping their answers either way.

Routing snapshots written from 2GIS answers get the Valhalla line and
elevation for the route's current stops, with a note in their warnings;
their other fields and the points already awarded stay as they are. A
snapshot whose route is gone just loses the 2GIS line.

Dry-run by default: prints the old and new length per route.

  uv run python scripts/rebuild_routes_osm.py
  uv run python scripts/rebuild_routes_osm.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, select, text

from tourism_backend.config import get_settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.admin.infrastructure import models as _admin_models  # noqa: F401
from tourism_backend.modules.favorites.infrastructure import models as _favorites  # noqa: F401
from tourism_backend.modules.geography.infrastructure import models as _geography  # noqa: F401
from tourism_backend.modules.identity.infrastructure import models as _identity  # noqa: F401
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.recommendations.infrastructure import (
    models as _recommendations,  # noqa: F401
)
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
from tourism_backend.modules.route_execution.infrastructure.models import RouteRoutingSnapshot
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop
from tourism_backend.modules.subscriptions.infrastructure import (
    models as _subscription_models,  # noqa: F401
)

_WALK_MODES = frozenset({"walk", "walking", "pedestrian", "foot"})
_REBUILT_NOTE = "geometry_rebuilt_osm"
_ROUTING_CACHE_PATTERN = "route-routing:*"


def _mode(route: Route) -> TransportMode:
    value = (route.transport_mode or "").casefold().strip()
    return "walk" if value in _WALK_MODES else "car"


async def _waypoints(session: Any, route_id: Any) -> list[RouteWaypoint]:
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route_id)
            .order_by(RouteStop.position)
        )
    ).all()
    return [
        RouteWaypoint(lng=float(lng), lat=float(lat), place_id=place_id)
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    ]


def _straight(waypoints: list[RouteWaypoint]) -> str:
    return "LINESTRING(" + ", ".join(f"{p.lng:.6f} {p.lat:.6f}" for p in waypoints) + ")"


def _store_route(
    route: Route,
    waypoints: list[RouteWaypoint],
    result: RoutingResult | None,
    data_version: str | None,
) -> None:
    meta = dict((route.accessibility or {}).get("routing") or {})
    for stale in ("legs", "steep_segment", "backfilled", "road_types"):
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


async def _clear_routing_cache(settings: Any) -> None:
    """Cached draft preview lines may still hold 2GIS answers."""
    redis = create_redis_client(settings)
    removed = 0
    async for key in redis.scan_iter(match=_ROUTING_CACHE_PATTERN):
        removed += await redis.delete(key)
    await redis.aclose()
    print(f"routing cache entries removed: {removed}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--snapshots",
        action="store_true",
        help="also replace the line and elevation of 2GIS routing snapshots",
    )
    args = parser.parse_args()
    settings = get_settings()
    if settings.routing_provider != "valhalla":
        raise SystemExit(f"routing_provider={settings.routing_provider!r}; expected valhalla")
    provider = get_routing_provider(settings)
    version = settings.osm_data_version
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    built = fallback = 0
    results: dict[Any, tuple[list[RouteWaypoint], RoutingResult | None]] = {}
    try:
        async with factory() as session:
            routes = list(
                (
                    await session.scalars(
                        select(Route)
                        .where(Route.lifecycle_status == "active")
                        .order_by(Route.created_at)
                    )
                ).all()
            )
            for route in routes:
                waypoints = await _waypoints(session, route.id)
                if len(waypoints) < 2:
                    continue
                old_provider = ((route.accessibility or {}).get("routing") or {}).get("provider")
                try:
                    result: RoutingResult | None = await provider.route(
                        waypoints=waypoints, transport_mode=_mode(route)
                    )
                except RoutingError as exc:
                    result = None
                    print(f"  straight [{exc.code}]: {route.name} (was {old_provider})")
                results[route.id] = (waypoints, result)
                if result is None:
                    fallback += 1
                else:
                    built += 1
                    print(
                        f"  ok: {route.name} {_mode(route)} "
                        f"{(route.distance_meters or 0) / 1000:.1f} -> "
                        f"{result.total_distance_meters / 1000:.1f} km (was {old_provider})"
                    )
                if args.apply:
                    _store_route(route, waypoints, result, version)

            # Routes first, in their own transaction: snapshots are guarded
            # by an immutability trigger and are handled separately.
            if args.apply:
                await session.commit()
            if not args.snapshots:
                print(
                    f"routes: built={built} straight={fallback}; snapshots untouched; "
                    f"mode={'apply' if args.apply else 'dry-run'}"
                )
                return
            snapshots = list(
                (
                    await session.scalars(
                        select(RouteRoutingSnapshot).where(RouteRoutingSnapshot.provider == "2gis")
                    )
                ).all()
            )
            for snapshot in snapshots:
                waypoints, result = results.get(snapshot.route_id, ([], None))
                print(
                    f"  snapshot {str(snapshot.id)[:8]}: "
                    f"{'valhalla line' if result else 'line removed'}"
                )
                if not args.apply:
                    continue
                notes = [*(snapshot.warnings or []), _REBUILT_NOTE]
                snapshot.warnings = notes
                snapshot.road_types = None
                snapshot.provider_version = version
                if result is not None and result.geometry_wkt:
                    snapshot.provider = result.provider
                    snapshot.geometry = WKTElement(result.geometry_wkt, srid=4326)
                    snapshot.elevation_gain_meters = result.elevation_gain_meters
                    snapshot.elevation_loss_meters = result.elevation_loss_meters
                    snapshot.min_altitude_meters = result.min_altitude_meters
                    snapshot.max_altitude_meters = result.max_altitude_meters
                    snapshot.max_road_angle_degrees = result.max_road_angle_degrees
                else:
                    snapshot.provider = None
                    snapshot.geometry = None
                    snapshot.elevation_gain_meters = None
                    snapshot.elevation_loss_meters = None
                    snapshot.min_altitude_meters = None
                    snapshot.max_altitude_meters = None
                    snapshot.max_road_angle_degrees = None
            if args.apply:
                # route_routing_snapshots_immutable forbids any UPDATE; this
                # one-off replacement of 2GIS lines (D14) lifts it inside this
                # transaction only, so a failure leaves the guard in place.
                with session.no_autoflush:
                    await session.execute(
                        text(
                            "ALTER TABLE route_routing_snapshots "
                            "DISABLE TRIGGER route_routing_snapshots_immutable"
                        )
                    )
                await session.flush()
                await session.execute(
                    text(
                        "ALTER TABLE route_routing_snapshots "
                        "ENABLE TRIGGER route_routing_snapshots_immutable"
                    )
                )
                await session.commit()
            print(
                f"routes: built={built} straight={fallback}; "
                f"2gis snapshots={len(snapshots)}; "
                f"mode={'apply' if args.apply else 'dry-run'}"
            )
    finally:
        await engine.dispose()
        if args.apply:
            await _clear_routing_cache(settings)


if __name__ == "__main__":
    asyncio.run(main())
