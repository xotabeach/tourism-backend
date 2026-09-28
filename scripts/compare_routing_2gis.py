#!/usr/bin/env python3
"""Compare Valhalla with 2GIS on our routes while the 2GIS key still works.

Spec 12a, section 4 (D15, D26). Every route with at least two stops is routed
live through both providers. 2GIS answers are kept in memory only for the
comparison and never written anywhere: the report holds only totals per
route (length and time difference in %, climb, share of the Valhalla line
within 30 m of the 2GIS line).

  uv run python scripts/compare_routing_2gis.py --valhalla http://valhalla:8002
  uv run python scripts/compare_routing_2gis.py --limit 5 --csv report.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys
from pathlib import Path

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, func, select

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    TwoGisRoutingProvider,
)
from tourism_backend.modules.route_builder.infrastructure.valhalla_routing import (
    ValhallaRoutingProvider,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

_NEAR_METERS = 30.0
# Same reading of Route.transport_mode as scripts/backfill_route_geometry.py.
_WALK_MODES = frozenset({"walk", "walking", "pedestrian", "foot"})


def _points(wkt: str | None) -> list[tuple[float, float]]:
    if not wkt or "(" not in wkt:
        return []
    body = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    return [tuple(map(float, part.split()[:2])) for part in body.split(",")]  # type: ignore[misc]


def _meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * 111_320 * math.cos(lat)
    dy = (a[1] - b[1]) * 110_540
    return math.hypot(dx, dy)


def _near_share(line: list[tuple[float, float]], reference: list[tuple[float, float]]) -> float:
    """Share of `line` points within 30 m of the `reference` polyline."""
    if not line or len(reference) < 2:
        return 0.0
    near = 0
    for point in line[:: max(1, len(line) // 400)]:
        best = min(
            _segment_distance(point, a, b) for a, b in zip(reference, reference[1:], strict=False)
        )
        near += best <= _NEAR_METERS
    return near / len(line[:: max(1, len(line) // 400)])


def _segment_distance(p, a, b) -> float:
    lat = math.radians(p[1])
    kx, ky = 111_320 * math.cos(lat), 110_540

    def xy(q):
        return (q[0] * kx, q[1] * ky)

    (px, py), (ax, ay), (bx, by) = xy(p), xy(a), xy(b)
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _pct(ours: int, theirs: int) -> str:
    return f"{(ours - theirs) / theirs * 100:+.0f}%" if theirs else "n/a"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valhalla", default=None, help="Valhalla URL (default from settings)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()
    settings = get_settings()
    key = settings.two_gis_http_api_key
    if key is None or not key.get_secret_value().strip():
        sys.exit("TWO_GIS_HTTP_API_KEY is not set: nothing to compare with")
    valhalla = ValhallaRoutingProvider(
        base_url=args.valhalla or settings.valhalla_base_url, timeout_seconds=20
    )
    two_gis = TwoGisRoutingProvider(
        api_key=key.get_secret_value(),
        base_url=settings.two_gis_routing_base_url,
        timeout_seconds=20,
        alternative=0,
        max_route_meters=None,
        filters=("dirt_road", "ferry"),
        daily_call_budget=settings.two_gis_daily_call_budget,
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    rows: list[dict[str, object]] = []
    geom = cast(Place.location, Geometry)
    async with factory() as session:
        routes = list(
            (
                await session.scalars(
                    select(Route)
                    .where(
                        Route.id.in_(
                            select(RouteStop.route_id)
                            .group_by(RouteStop.route_id)
                            .having(func.count() >= 2)
                        ),
                        Route.lifecycle_status == "active",
                    )
                    .order_by(Route.created_at)
                    .limit(args.limit)
                )
            ).all()
        )
        for route in routes:
            stops = (
                await session.execute(
                    select(ST_X(geom), ST_Y(geom))
                    .join(RouteStop, RouteStop.place_id == Place.id)
                    .where(RouteStop.route_id == route.id)
                    .order_by(RouteStop.position)
                )
            ).all()
            waypoints = [RouteWaypoint(float(x), float(y)) for x, y in stops]
            value = (route.transport_mode or "").casefold().strip()
            mode: TransportMode = "walk" if value in _WALK_MODES else "car"
            row: dict[str, object] = {"route": route.name[:40], "mode": mode, "stops": len(stops)}
            results: dict[str, RoutingResult | str] = {}
            for name, provider in (("valhalla", valhalla), ("2gis", two_gis)):
                try:
                    results[name] = await provider.route(waypoints=waypoints, transport_mode=mode)
                except RoutingError as exc:
                    results[name] = exc.code
            ours, theirs = results["valhalla"], results["2gis"]
            if isinstance(ours, RoutingResult) and isinstance(theirs, RoutingResult):
                near = _near_share(_points(ours.geometry_wkt), _points(theirs.geometry_wkt))
                row |= {
                    "km_valhalla": round(ours.total_distance_meters / 1000, 1),
                    "km_2gis": round(theirs.total_distance_meters / 1000, 1),
                    "length": _pct(ours.total_distance_meters, theirs.total_distance_meters),
                    "time": _pct(ours.total_duration_seconds, theirs.total_duration_seconds),
                    "climb_valhalla": ours.elevation_gain_meters,
                    "climb_2gis": theirs.elevation_gain_meters,
                    "near_30m": f"{near:.0%}",
                }
            else:
                row |= {
                    "valhalla": ours if isinstance(ours, str) else "ok",
                    "2gis": theirs if isinstance(theirs, str) else "ok",
                }
            rows.append(row)
            print(row, flush=True)
            # 2GIS answers go out of scope here; nothing of theirs is kept.
            del results, ours, theirs
            # The demo key allows 5 calls a minute and a walk is sent in chunks
            # of up to 5 points; exceeding that blocks the key for a while.
            chunks = max(1, math.ceil((len(waypoints) - 1) / 4))
            await asyncio.sleep(13 * chunks)
    await engine.dispose()
    if args.csv:
        with args.csv.open("w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=sorted({k for r in rows for k in r}))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    asyncio.run(main())
