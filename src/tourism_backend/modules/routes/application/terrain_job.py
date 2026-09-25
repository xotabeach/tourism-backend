"""Fetch the ground under routes in the background (spec 17, D23).

Saving a route estimates its difficulty at once from what it has; the
trail grades and road surfaces come from Valhalla afterwards, one route at
a time, so an author's save never waits on them and a bulk recalculation
does not crowd the shared CPU. A route is picked when its stops changed
since the last fetch or the fetch rules got a new version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tourism_backend.config import Settings
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.infrastructure.valhalla_terrain import (
    TERRAIN_VERSION,
    ValhallaTerrainClient,
)
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.infrastructure.models import Route, RouteSegment, RouteStop

_logger = logging.getLogger("tourism_backend.terrain_job")

# One process at a time sweeps (several uvicorn workers may run this).
_LOCK_KEY = 170_017
_BATCH = 5
_PAUSE_BETWEEN_ROUTES_SECONDS = 1.0
_TERRAIN_MODES = ("walk", "car")

# Routes whose ground is missing or older than their stops or the rules.
_STALE = text(
    """
    coalesce(routes.accessibility->'routing'->>'synthetic', 'true') = 'false'
    AND routes.lifecycle_status IS DISTINCT FROM 'deleted'
    AND (
      routes.accessibility->'terrain' IS NULL
      OR (routes.accessibility->'terrain'->>'version')::int < :version
      OR (routes.accessibility->'terrain'->'place_ids')
         IS DISTINCT FROM (routes.accessibility->'routing'->'place_ids')
    )
    """
).bindparams(version=TERRAIN_VERSION)


async def fetch_route_terrain(
    session: AsyncSession, route: Route, client: ValhallaTerrainClient
) -> bool:
    """Fetch and store the ground of every walked or driven segment, then
    recompute the difficulty. False when Valhalla matched nothing."""
    rows = (
        await session.execute(
            select(
                RouteSegment.leg_index,
                RouteSegment.seq,
                RouteSegment.mode,
                func.ST_AsGeoJSON(RouteSegment.geometry),
            )
            .where(RouteSegment.route_id == route.id, RouteSegment.mode.in_(_TERRAIN_MODES))
            .order_by(RouteSegment.leg_index, RouteSegment.seq)
        )
    ).all()
    segments: list[dict[str, Any]] = []
    matched = 0
    legs = await _leg_lines(session, route)
    for leg_index, seq, mode, geojson in rows:
        line = json.loads(geojson).get("coordinates") if geojson else None
        if not line and seq == 0:
            # Walking routes keep one line for the whole route: cut the
            # leg out of it between its two stops.
            line = legs.get(leg_index)
        if not line:
            continue
        meters = await client.segment_ground(line, mode=mode)
        if meters is None:
            continue
        matched += 1
        segments.append({"leg_index": leg_index, "seq": seq, "meters": meters})
    accessibility = dict(route.accessibility or {})
    routing = accessibility.get("routing") if isinstance(accessibility.get("routing"), dict) else {}
    # Stored even when nothing matched, so the route is not picked again
    # until its stops change; its estimate stays «примерно».
    accessibility["terrain"] = {
        "version": TERRAIN_VERSION,
        "place_ids": (routing or {}).get("place_ids"),
        "computed_at": datetime.now(UTC).isoformat(),
        "segments": segments,
    }
    route.accessibility = accessibility
    await refresh_route_structure(session, route)
    return matched > 0


async def _leg_lines(session: AsyncSession, route: Route) -> dict[int, list[list[float]]]:
    """Each leg's piece of the route line, cut at the vertices nearest the stops."""
    geojson = await session.scalar(
        select(func.ST_AsGeoJSON(Route.geometry)).where(Route.id == route.id)
    )
    line = json.loads(geojson).get("coordinates") if geojson else None
    if not line or len(line) < 2:
        return {}
    geom = cast(Place.location, Geometry)
    stops = (
        await session.execute(
            select(ST_X(geom), ST_Y(geom))
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    return split_at_stops(line, [(float(x), float(y)) for x, y in stops])


def split_at_stops(
    line: list[list[float]], stops: list[tuple[float, float]]
) -> dict[int, list[list[float]]]:
    """Cut a [lng, lat] line into legs at the vertex nearest each stop, in order."""
    cuts: list[int] = []
    start = 0
    for lng, lat in stops:
        best, best_d = start, float("inf")
        for index in range(start, len(line)):
            d = (line[index][0] - lng) ** 2 + (line[index][1] - lat) ** 2
            if d < best_d:
                best, best_d = index, d
        cuts.append(best)
        start = best
    return {
        leg: line[cuts[leg] : cuts[leg + 1] + 1]
        for leg in range(len(cuts) - 1)
        if cuts[leg + 1] > cuts[leg]
    }


async def sweep_once(
    session_factory: async_sessionmaker[AsyncSession], client: ValhallaTerrainClient
) -> int:
    """Fetch the ground for a few stale routes; returns how many were done."""
    async with session_factory() as session:
        got_lock = await session.scalar(select(func.pg_try_advisory_lock(_LOCK_KEY)))
        if not got_lock:
            return 0
        try:
            ids: list[UUID] = list(
                (
                    await session.scalars(
                        select(Route.id).where(_STALE).order_by(Route.updated_at).limit(_BATCH)
                    )
                ).all()
            )
            done = 0
            for route_id in ids:
                route = await session.get(Route, route_id)
                if route is None:
                    continue
                try:
                    await fetch_route_terrain(session, route, client)
                    await session.commit()
                    done += 1
                except Exception:  # noqa: BLE001 — one bad route must not stop the rest
                    await session.rollback()
                    _logger.exception("route_terrain_failed", extra={"route_id": str(route_id)})
                await asyncio.sleep(_PAUSE_BETWEEN_ROUTES_SECONDS)
            return done
        finally:
            await session.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            await session.commit()


async def poll_route_terrain(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    if settings.routing_provider != "valhalla":
        return
    client = ValhallaTerrainClient(base_url=settings.valhalla_base_url)
    while True:
        try:
            done = await sweep_once(session_factory, client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed sweep must not stop later ones
            _logger.exception("route_terrain_sweep_failed")
            done = 0
        # Busy while there is a backlog, idle otherwise.
        await asyncio.sleep(2 if done else settings.terrain_poll_interval_seconds)


async def stop_terrain_poller(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
