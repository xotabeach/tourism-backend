"""Car parks from the OSM build: loading and lookup (spec 14b, section 1).

The build writes ``parkings.geojson`` next to the Valhalla graph. Each data
version replaces the table as a whole; nothing in it is edited by hand.

  docker compose exec -T backend python -m \\
      tourism_backend.modules.route_builder.infrastructure.parkings osm20260922 \\
      < osm/current/parkings.geojson
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from geoalchemy2 import Geography, Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.route_builder.infrastructure.models import Parking

_logger = logging.getLogger("tourism_backend.parkings")

# Candidates for one stop: within 2 km, the nearest few (spec 14, D6).
PARKING_RADIUS_METERS = 2_000
PARKING_CANDIDATES = 5
_BATCH = 500


@dataclass(frozen=True, slots=True)
class ParkingPoint:
    osm_id: str
    lng: float
    lat: float


def parse_features(collection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows for the table; malformed features are skipped, not fatal."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in collection.get("features") or []:
        if not isinstance(feature, Mapping):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
        osm_id = props.get("osm_id") if isinstance(props, Mapping) else None
        if (
            not isinstance(osm_id, str)
            or osm_id in seen
            or not isinstance(coords, list)
            or len(coords) < 2
            or not all(isinstance(c, (int, float)) and math.isfinite(c) for c in coords[:2])
        ):
            continue
        lng, lat = float(coords[0]), float(coords[1])
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            continue
        seen.add(osm_id)
        capacity = props.get("capacity")
        rows.append(
            {
                "osm_id": osm_id[:32],
                "lng": lng,
                "lat": lat,
                "access": _short(props.get("access")),
                "fee": _short(props.get("fee")),
                "capacity": capacity if isinstance(capacity, int) and capacity >= 0 else None,
                "name": str(props["name"])[:255] if props.get("name") else None,
            }
        )
    return rows


def _short(value: object) -> str | None:
    return str(value)[:32] if value else None


async def replace_parkings(
    session: AsyncSession, rows: Iterable[Mapping[str, Any]], *, data_version: str
) -> int:
    """Swap the whole table for this data version inside the caller's transaction."""
    now = datetime.now(UTC)
    await session.execute(delete(Parking))
    batch: list[dict[str, Any]] = []
    count = 0
    for row in rows:
        batch.append(
            {
                "id": uuid4(),
                "osm_id": row["osm_id"],
                "location": WKTElement(f"POINT({row['lng']} {row['lat']})", srid=4326),
                "access": row["access"],
                "fee": row["fee"],
                "capacity": row["capacity"],
                "name": row["name"],
                "data_version": data_version,
                "created_at": now,
                "updated_at": now,
            }
        )
        if len(batch) >= _BATCH:
            await session.execute(insert(Parking), batch)
            count += len(batch)
            batch = []
    if batch:
        await session.execute(insert(Parking), batch)
        count += len(batch)
    return count


async def nearest_parkings(
    session: AsyncSession,
    *,
    lng: float,
    lat: float,
    radius_meters: int = PARKING_RADIUS_METERS,
    limit: int = PARKING_CANDIDATES,
) -> list[ParkingPoint]:
    """Car parks around a point, nearest first (straight-line distance)."""
    point = cast(func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326), Geography)
    geom = cast(Parking.location, Geometry)
    rows = (
        await session.execute(
            select(Parking.osm_id, ST_X(geom), ST_Y(geom))
            .where(func.ST_DWithin(Parking.location, point, radius_meters))
            .order_by(func.ST_Distance(Parking.location, point))
            .limit(limit)
        )
    ).all()
    return [ParkingPoint(osm_id, float(x), float(y)) for osm_id, x, y in rows]


async def _main(data_version: str) -> None:
    from tourism_backend.config import get_settings
    from tourism_backend.db.session import create_engine, create_session_factory

    rows = parse_features(json.load(sys.stdin))
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            count = await replace_parkings(session, rows, data_version=data_version)
            await session.commit()
    finally:
        await engine.dispose()
    sys.stdout.write(f"parkings loaded: {count} ({data_version})\n")


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))


class ParkingIndex:
    """All car parks in memory, for lookups while a route is routed.

    A few thousand points: a linear scan is cheaper than a database round
    trip per stop, and the router has no session to hand.
    """

    def __init__(self, points: Iterable[ParkingPoint]) -> None:
        self._points = list(points)

    def __len__(self) -> int:
        return len(self._points)

    def nearest(
        self,
        lng: float,
        lat: float,
        *,
        radius_meters: int = PARKING_RADIUS_METERS,
        limit: int = PARKING_CANDIDATES,
    ) -> list[ParkingPoint]:
        # Cheap box first: a degree of latitude is ~111 km everywhere.
        dlat = radius_meters / 111_000
        dlng = dlat / max(0.1, math.cos(math.radians(lat)))
        near = [
            (_haversine(lng, lat, p.lng, p.lat), p)
            for p in self._points
            if abs(p.lat - lat) <= dlat and abs(p.lng - lng) <= dlng
        ]
        near = [item for item in near if item[0] <= radius_meters]
        near.sort(key=lambda item: item[0])
        return [p for _, p in near[:limit]]


def _haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


_INDEX_TTL_SECONDS = 3600.0
_index_cache: tuple[float, ParkingIndex] | None = None
_index_lock = asyncio.Lock()


async def parking_index(settings: Any) -> ParkingIndex:
    """The car parks of the current data version, reloaded hourly.

    An unreadable table gives an empty index: routes then fall back to the
    street a car reaches, which is what they did before car parks existed.
    """
    global _index_cache
    now = time.monotonic()
    if _index_cache is not None and now - _index_cache[0] < _INDEX_TTL_SECONDS:
        return _index_cache[1]
    async with _index_lock:
        if _index_cache is not None and now - _index_cache[0] < _INDEX_TTL_SECONDS:
            return _index_cache[1]
        from tourism_backend.db.session import create_engine, create_session_factory

        engine = create_engine(settings)
        geom = cast(Parking.location, Geometry)
        try:
            async with create_session_factory(engine)() as session:
                rows = (await session.execute(select(Parking.osm_id, ST_X(geom), ST_Y(geom)))).all()
            index = ParkingIndex(ParkingPoint(o, float(x), float(y)) for o, x, y in rows)
        except Exception:  # noqa: BLE001 — a missing table must not stop routing
            _logger.warning("parking_index_unavailable", exc_info=True)
            index = ParkingIndex(())
        finally:
            await engine.dispose()
        _index_cache = (time.monotonic(), index)
        return index
