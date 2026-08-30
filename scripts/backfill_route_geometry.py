#!/usr/bin/env python3
"""Recompute stored route geometry through the configured routing provider.

Switching ``ROUTING_PROVIDER`` to 2gis only affects newly generated routes:
everything created earlier keeps the straight-line synthetic geometry it was
saved with, which is why older routes still render as a diagonal line across
the map. This walks existing routes and replaces that geometry with the
provider's road-following one.

Dry-run by default. Each converted route costs provider calls, so the run is
bounded by ``--limit`` and skips routes that already have provider geometry
unless ``--force`` is given.

Examples:
  uv run python scripts/backfill_route_geometry.py --limit 5
  uv run python scripts/backfill_route_geometry.py --limit 5 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.places.infrastructure import models as _places_models
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.recommendations.infrastructure import models as _reco_models
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    TransportMode,
)
from tourism_backend.modules.route_builder.infrastructure.routing_factory import (
    get_routing_provider,
)
from tourism_backend.modules.route_execution.infrastructure import models as _execution_models
from tourism_backend.modules.routes.infrastructure import models as _routes_models
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

_ = (
    _reco_models,
    _geography_models,
    _identity_models,
    _places_models,
    _routes_models,
    _favorites_models,
    _execution_models,
)

_WALK_MODES = {"walk", "walking", "pedestrian", "foot"}


def _transport_mode(route: Route) -> TransportMode:
    value = (route.transport_mode or "").casefold().strip()
    return "walk" if value in _WALK_MODES else "car"


async def _waypoints(session: AsyncSession, route_id: Any) -> list[RouteWaypoint]:
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom), Place.name, RouteStop.position)
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route_id)
            .order_by(RouteStop.position)
        )
    ).all()
    return [
        RouteWaypoint(lng=float(lng), lat=float(lat), place_id=place_id, label=name)
        for place_id, lng, lat, name, _position in rows
        if lng is not None and lat is not None
    ]


def _has_provider_geometry(route: Route) -> bool:
    routing = route.accessibility.get("routing") if isinstance(route.accessibility, dict) else None
    if not isinstance(routing, dict):
        return False
    return routing.get("synthetic") is False and bool(routing.get("geometry_available"))


async def _run(*, limit: int, apply: bool, force: bool) -> None:
    settings = get_settings()
    if settings.routing_provider != "2gis":
        raise SystemExit(
            f"routing_provider={settings.routing_provider!r}; "
            "set ROUTING_PROVIDER=2gis before backfilling"
        )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = get_routing_provider(settings)
    converted = 0
    skipped = 0
    failed = 0
    try:
        async with factory() as session:
            stop_counts = (
                select(RouteStop.route_id, func.count().label("stops"))
                .group_by(RouteStop.route_id)
                .subquery()
            )
            routes = list(
                (
                    await session.scalars(
                        select(Route)
                        .join(stop_counts, stop_counts.c.route_id == Route.id)
                        .where(
                            Route.publication_status == "published",
                            stop_counts.c.stops >= 2,
                        )
                        .order_by(Route.created_at)
                        .limit(limit)
                    )
                ).all()
            )
            print(f"candidates: {len(routes)}")
            for route in routes:
                if not force and _has_provider_geometry(route):
                    skipped += 1
                    print(f"  skip (already provider geometry): {route.name}")
                    continue
                waypoints = await _waypoints(session, route.id)
                if len(waypoints) < 2:
                    skipped += 1
                    print(f"  skip (no coordinates): {route.name}")
                    continue
                mode = _transport_mode(route)
                try:
                    result = await provider.route(waypoints=waypoints, transport_mode=mode)
                except RoutingError as exc:
                    failed += 1
                    print(f"  fail [{exc.code}]: {route.name}")
                    continue
                if result.synthetic or not result.geometry_wkt:
                    failed += 1
                    print(f"  fail (provider returned no road geometry): {route.name}")
                    continue
                converted += 1
                print(
                    f"  ok: {route.name} mode={mode} "
                    f"distance_m={result.total_distance_meters} "
                    f"points={result.geometry_wkt.count(',') + 1}"
                )
                if not apply:
                    continue
                route.geometry = WKTElement(result.geometry_wkt, srid=4326)
                route.distance_meters = result.total_distance_meters
                accessibility = dict(route.accessibility or {})
                routing_meta = dict(accessibility.get("routing") or {})
                routing_meta.update(
                    {
                        "provider": result.provider,
                        "synthetic": False,
                        "geometry_available": True,
                        "road_types": list(result.road_types),
                        "backfilled": True,
                    }
                )
                accessibility["routing"] = routing_meta
                route.accessibility = accessibility
            if apply:
                await session.commit()
    finally:
        await engine.dispose()
    mode_label = "applied" if apply else "dry-run"
    print(
        f"route_geometry_backfill[{mode_label}]: "
        f"converted={converted} skipped={skipped} failed={failed}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--apply", action="store_true", help="persist the new geometry")
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute even when provider geometry is already stored",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("limit must be between 1 and 100")
    asyncio.run(_run(limit=args.limit, apply=args.apply, force=args.force))


if __name__ == "__main__":
    main()
