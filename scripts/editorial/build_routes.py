#!/usr/bin/env python3
"""Build editorial routes from approved ideas (spec 16a, section 3, D31, D33, D40).

Reads JSON lines from stdin, one approved idea each:

  {"key", "title", "story", "stops": [place ids in order],
   "day_breaks": [place ids that end a day] | [], "transport": "walk"|"car",
   "wow": place id, "find": place id | null, "axes": {...}}

For every idea the route is created (or rebuilt, found by its key) as a
draft of the КРЫМТРИП profile, never shown in the catalog before launch:
stops in order with visit minutes, the path through the configured router
(Valhalla on production), days, segments and difficulty (specs 14, 17), and
the cover from the wow point's photo. Then the realism checks run and a JSON
report line per idea goes to stdout for the owner's review and the rework
loop. Dry-run (everything rolled back) unless --apply.

  cat ideas.jsonl | docker compose exec -T backend python \\
      scripts/editorial/build_routes.py --apply > report.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from sqlalchemy import cast, delete, func, select

import tourism_backend.main  # noqa: F401  (every model, for the mapper)
from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.application.content_enrichment import slugify_name
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceCategory,
    PlaceImage,
)
from tourism_backend.modules.routes.application.rerouting import reroute_route
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)

EDITORIAL_PHONE = "+000000000001"
# Visit minutes when a place has none (D40), by its first matching category.
VISIT_MINUTES = {
    "museum": 90,
    "palace": 90,
    "fortress": 60,
    "cave": 60,
    "park": 45,
    "beach": 60,
    "mountain": 30,
    "nature": 30,
    "landmark": 30,
    "religious_site": 20,
    "viewpoint": 20,
    "monument": 15,
}
DEFAULT_VISIT = 30
LUNCH_MINUTES = 60
# Day limits by difficulty (D31): hours of the day, walking km, driving hours.
LIMITS = {1: (6, 8, 4), 2: (6, 8, 4), 3: (8, 15, 4), 4: (10, 22, 4), 5: (10, 22, 4)}
DETOUR = 1.3
# Owner's call 2026-09-29: up to 1.5 h over the day limit is a «насыщенный
# день» shown on the review page, not a rejection.
FULL_DAY_SLACK = 1.5


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lng1, lat1), (lng2, lat2) = a, b
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(h))


def _path_km(points: list[tuple[float, float]]) -> float:
    return sum(_km(a, b) for a, b in zip(points, points[1:], strict=False))


def _best_open_path_km(points: list[tuple[float, float]]) -> float:
    """Shortest order through the same points with the same start, by
    nearest neighbour plus 2-opt: close enough to spot a zigzag."""
    order = [0]
    rest = list(range(1, len(points)))
    while rest:
        last = points[order[-1]]
        nxt = min(rest, key=lambda i: _km(last, points[i]))
        order.append(nxt)
        rest.remove(nxt)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if _path_km([points[k] for k in candidate]) + 1e-9 < _path_km(
                    [points[k] for k in order]
                ):
                    order, improved = candidate, True
    return _path_km([points[k] for k in order])


async def _visit_minutes(session: Any, place: Place) -> int:
    if place.recommended_visit_minutes:
        return int(place.recommended_visit_minutes)
    codes = set(
        await session.scalars(
            select(Category.code)
            .join(PlaceCategory, PlaceCategory.category_id == Category.id)
            .where(PlaceCategory.place_id == place.id)
        )
    )
    return next((m for code, m in VISIT_MINUTES.items() if code in codes), DEFAULT_VISIT)


async def _build(session: Any, idea: dict[str, Any], owner: UUID, region: UUID) -> dict[str, Any]:
    key = idea["key"]
    report: dict[str, Any] = {
        "key": key,
        "title": idea["title"],
        "problems": [],
        "warnings": [],
    }
    places = []
    for place_id in idea["stops"]:
        place = await session.get(Place, UUID(place_id))
        if place is None or place.publication_status != "published":
            report["problems"].append(f"точка не опубликована: {place_id}")
            return report
        places.append(place)

    route = await session.scalar(
        select(Route).where(Route.accessibility["editorial"]["key"].astext == key)
    )
    if route is None:
        digest = hashlib.sha256(key.encode()).hexdigest()[:6]
        route = Route(
            id=uuid4(),
            region_id=region,
            slug=f"{slugify_name(idea['title'], max_length=60)}-{digest}",
            source="editorial",
            visibility="public",
            lifecycle_status="active",
            publication_status="draft",
            freshness_status="fresh",
        )
        session.add(route)
        report["action"] = "created"
    else:
        await session.execute(delete(RouteSegment).where(RouteSegment.route_id == route.id))
        await session.execute(delete(RouteDay).where(RouteDay.route_id == route.id))
        await session.execute(delete(RouteStop).where(RouteStop.route_id == route.id))
        report["action"] = "rebuilt"
    route.owner_user_id = owner
    route.name = idea["title"]
    route.short_description = idea.get("story")
    route.transport_mode = "walk" if idea.get("transport") == "walk" else "car"
    breaks = [str(b) for b in idea.get("day_breaks") or []]
    route.days_manual = bool(breaks)
    route.day_breaks = breaks or None
    route.accessibility = {
        **(route.accessibility or {}),
        "editorial": {
            "key": key,
            "story": idea.get("story"),
            "wow": idea.get("wow"),
            "find": idea.get("find"),
            "axes": idea.get("axes") or {},
        },
    }
    await session.flush()

    visits = []
    for position, place in enumerate(places, start=1):
        minutes = await _visit_minutes(session, place)
        visits.append(minutes)
        session.add(
            RouteStop(
                route_id=route.id,
                place_id=place.id,
                position=position,
                visit_duration_minutes=minutes,
            )
        )
    await session.flush()
    result = await reroute_route(session, route)
    report["route_id"] = str(route.id)
    report["routed"] = result is not None

    wow = UUID(idea["wow"]) if idea.get("wow") else places[0].id
    cover = await session.scalar(
        select(PlaceImage.id)
        .where(
            PlaceImage.place_id == wow,
            PlaceImage.status == "active",
            PlaceImage.kind == "photo",
        )
        .order_by(PlaceImage.is_cover.desc(), PlaceImage.sort_order)
        .limit(1)
    )
    route.cover_place_image_id = cover
    if cover is None:
        report["problems"].append("у вау-точки нет фото")

    # Realism (D31): walking km, driving hours and the whole day per day.
    stops = (
        await session.scalars(
            select(RouteStop).where(RouteStop.route_id == route.id).order_by(RouteStop.position)
        )
    ).all()
    position_of = {s.id: s.position for s in stops}
    segments = (
        await session.scalars(select(RouteSegment).where(RouteSegment.route_id == route.id))
    ).all()
    days = (
        await session.scalars(
            select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
        )
    ).all()
    # Nights named in the idea (D32) replace «Ночлег в районе: <stop>».
    for day, night in zip(days, idea.get("nights") or [], strict=False):
        if night:
            day.overnight_note = f"Ночлег: {night}"
    level = route.difficulty_level or 3
    hours_cap, walk_cap, drive_cap = LIMITS.get(level, LIMITS[3])
    report["difficulty"] = level
    report["distance_km"] = round((route.distance_meters or 0) / 1000, 1)
    report["days"] = []
    for day in days:
        first, last = position_of[day.first_stop_id], position_of[day.last_stop_id]
        inside = [
            s
            for s in segments
            if first <= position_of.get(s.from_stop_id, 0) < last
            and position_of.get(s.to_stop_id, 0) <= last
        ]
        walk_km = sum((s.distance_meters or 0) for s in inside if s.mode == "walk") / 1000
        drive_h = sum((s.duration_seconds or 0) for s in inside if s.mode == "car") / 3600
        move_h = sum((s.duration_seconds or 0) for s in inside) / 3600
        visit_h = sum(visits[first - 1 : last]) / 60
        day_h = move_h + visit_h + LUNCH_MINUTES / 60
        report["days"].append(
            {
                "day": day.day_index,
                "stops": last - first + 1,
                "walk_km": round(walk_km, 1),
                "drive_h": round(drive_h, 1),
                "day_h": round(day_h, 1),
                "night": day.overnight_note,
            }
        )
        if day_h > hours_cap + FULL_DAY_SLACK:
            report["problems"].append(
                f"день {day.day_index}: {day_h:.1f} ч, для сложности {level} предел {hours_cap} ч"
            )
        elif day_h > hours_cap:
            report["warnings"].append(
                f"день {day.day_index}: насыщенный день, {day_h:.1f} ч при пределе {hours_cap} ч"
            )
        if walk_km > walk_cap:
            report["problems"].append(
                f"день {day.day_index}: пешком {walk_km:.1f} км, предел {walk_cap} км"
            )
        if drive_h > drive_cap:
            report["problems"].append(
                f"день {day.day_index}: за рулём {drive_h:.1f} ч, предел {drive_cap} ч"
            )
    points = []
    for place in places:
        point = cast(Place.location, Geometry)
        lng_lat = await session.execute(
            select(func.ST_X(point), func.ST_Y(point)).where(Place.id == place.id)
        )
        points.append(tuple(lng_lat.one()))
    # A multi-day route keeps its days: its order is checked day by day.
    groups, current = [], []
    for place, point in zip(places, points, strict=True):
        current.append(point)
        if str(place.id) in breaks:
            groups.append(current)
            current = []
    groups.append(current)
    for number, group in enumerate(groups, start=1):
        chosen, best = _path_km(group), _best_open_path_km(group)
        if best > 1 and chosen > DETOUR * best:
            where = f"день {number}: " if len(groups) > 1 else ""
            report["problems"].append(
                f"{where}зигзаг: порядок точек {chosen:.0f} км по прямой, можно {best:.0f} км"
            )
    if not report["routed"]:
        report["problems"].append("путь не построен, прямые линии")
    return report


async def main(apply: bool) -> None:
    ideas = [json.loads(line) for line in sys.stdin if line.strip()]
    engine = create_engine(get_settings())
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            owner = await session.scalar(select(User.id).where(User.phone_e164 == EDITORIAL_PHONE))
            region = await session.scalar(select(Region.id).where(Region.slug == "crimea"))
            if owner is None or region is None:
                raise SystemExit("КРЫМТРИП profile or the Crimea region is missing")
            for idea in ideas:
                async with session.begin_nested():
                    report = await _build(session, idea, owner, region)
                print(json.dumps(report, ensure_ascii=False), flush=True)
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    print(f"build_routes[{'applied' if apply else 'dry-run'}]: {len(ideas)} ideas", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
