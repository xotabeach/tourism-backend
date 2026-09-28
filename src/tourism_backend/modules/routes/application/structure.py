"""Keep a route's days and segments in step with its stops and routing.

Step 0 of spec 14: the rows are derived from what the route already has
(``accessibility.routing.legs`` and the stop order), so every write path that
changes stops or routing stays as it is. The run start refreshes them before
the snapshot is taken, and ``scripts/backfill_route_structure.py`` fills them
for existing routes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.polyline import decode_polyline6
from tourism_backend.modules.route_builder.application.routing import RoutingError
from tourism_backend.modules.routes.application.day_split import (
    DayStop,
    day_norm_minutes,
    split_days,
)
from tourism_backend.modules.routes.application.difficulty import (
    DayInput,
    RouteDifficulty,
    SegmentInput,
    legacy_name,
    route_difficulty,
    shown_level,
)
from tourism_backend.modules.routes.application.structure_rules import (
    PlannedDay,
    PlannedSegment,
    base_mode_for,
    days_from_breaks,
    implies_public_transport,
    plan_segments,
    segment_mode_for,
    segment_shapes,
)
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)

# A stop with no visit time given, as the AI chat assumes (generate_service).
_DEFAULT_VISIT_MINUTES = 45
# Straight-line guesses for legs the router has no numbers for.
_WALK_MPS = 1.25
_DRIVE_MPS = 11.1


def _routing(route: Route) -> dict[str, Any]:
    value = route.accessibility
    routing = value.get("routing") if isinstance(value, dict) else None
    return routing if isinstance(routing, dict) else {}


def _line(shape: str | None) -> WKTElement | None:
    if not shape:
        return None
    try:
        points = decode_polyline6(shape)
    except RoutingError:
        return None
    if len(points) < 2:
        return None
    return WKTElement(
        "LINESTRING(" + ", ".join(f"{lng:.6f} {lat:.6f}" for lng, lat in points) + ")",
        srid=4326,
    )


def _as_planned_segment(row: RouteSegment) -> PlannedSegment:
    return PlannedSegment(
        leg_index=row.leg_index,
        seq=row.seq,
        from_stop_id=row.from_stop_id,
        to_stop_id=row.to_stop_id,
        mode=row.mode,  # type: ignore[arg-type]
        role=row.role,  # type: ignore[arg-type]
        origin=row.origin,  # type: ignore[arg-type]
        distance_meters=row.distance_meters,
        duration_seconds=row.duration_seconds,
        elevation_gain_meters=row.elevation_gain_meters,
        elevation_loss_meters=row.elevation_loss_meters,
    )


def _as_planned_day(row: RouteDay) -> PlannedDay:
    return PlannedDay(
        day_index=row.day_index,
        first_stop_id=row.first_stop_id,
        last_stop_id=row.last_stop_id,
        boundary_source=row.boundary_source,  # type: ignore[arg-type]
        overloaded=row.overloaded,
        overnight_note=row.overnight_note,
    )


@dataclass(frozen=True, slots=True)
class RouteStructure:
    segments: list[PlannedSegment]
    days: list[PlannedDay]
    #: Days the norms give, whatever the author set (spec 14, D20).
    auto_day_count: int
    #: Estimate by the route's days, and by the days the norms give (spec 17).
    difficulty: RouteDifficulty | None = None
    reward_difficulty: RouteDifficulty | None = None


async def refresh_route_structure(
    session: AsyncSession,
    route: Route,
) -> RouteStructure:
    """Bring the route's segments and days up to date; return them in order.

    Rows are rewritten only when something changed, so a run start on an
    untouched route writes nothing. A leg an editor set by hand is kept as
    it is (spec 14, D3); so are days once someone moved a boundary (D8).
    """

    route.base_mode = base_mode_for(route.transport_mode)
    if implies_public_transport(route.transport_mode):
        route.needs_public_transport = True

    geom = cast(Place.location, Geometry)
    stop_rows = (
        await session.execute(
            select(
                RouteStop.id,
                Place.name,
                RouteStop.visit_duration_minutes,
                RouteStop.time_of_day,
                ST_X(geom),
                ST_Y(geom),
                RouteStop.place_id,
            )
            .join(Place, Place.id == RouteStop.place_id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    stop_ids = [row[0] for row in stop_rows]
    existing_segments = list(
        (
            await session.scalars(
                select(RouteSegment)
                .where(RouteSegment.route_id == route.id)
                .order_by(RouteSegment.leg_index, RouteSegment.seq)
            )
        ).all()
    )
    existing_days = list(
        (
            await session.scalars(
                select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
            )
        ).all()
    )

    routing = _routing(route)
    planned = plan_segments(stop_ids, routing=routing, base_mode=route.base_mode)
    shapes = segment_shapes(routing)
    edited: dict[tuple[UUID, UUID], list[RouteSegment]] = {}
    for row in existing_segments:
        if row.origin == "editor":
            edited.setdefault((row.from_stop_id, row.to_stop_id), [])
    for row in existing_segments:
        pair = (row.from_stop_id, row.to_stop_id)
        if pair in edited:
            edited[pair].append(row)
    segments: list[PlannedSegment] = []
    for segment in planned:
        kept = edited.get((segment.from_stop_id, segment.to_stop_id))
        if kept is None:
            segments.append(segment)
        else:
            segments.extend(
                replace(_as_planned_segment(row), leg_index=segment.leg_index) for row in kept
            )

    if [_as_planned_segment(row) for row in existing_segments] != segments:
        await session.execute(delete(RouteSegment).where(RouteSegment.route_id == route.id))
        for segment in segments:
            session.add(
                RouteSegment(
                    id=uuid4(),
                    route_id=route.id,
                    leg_index=segment.leg_index,
                    seq=segment.seq,
                    from_stop_id=segment.from_stop_id,
                    to_stop_id=segment.to_stop_id,
                    mode=segment.mode,
                    role=segment.role,
                    origin=segment.origin,
                    distance_meters=segment.distance_meters,
                    duration_seconds=segment.duration_seconds,
                    elevation_gain_meters=segment.elevation_gain_meters,
                    elevation_loss_meters=segment.elevation_loss_meters,
                    geometry=_line(shapes.get((segment.leg_index, segment.seq))),
                )
            )

    automatic = _auto_days(route, stop_rows, segments)
    days = (
        # Kept by place: the stops are new rows after every author save.
        days_from_breaks([(row[0], row[6], row[1]) for row in stop_rows], route.day_breaks or [])
        if route.days_manual and stop_rows
        else automatic
    )
    if [_as_planned_day(row) for row in existing_days] != days:
        await session.execute(delete(RouteDay).where(RouteDay.route_id == route.id))
        for day in days:
            session.add(
                RouteDay(
                    id=uuid4(),
                    route_id=route.id,
                    day_index=day.day_index,
                    first_stop_id=day.first_stop_id,
                    last_stop_id=day.last_stop_id,
                    boundary_source=day.boundary_source,
                    overloaded=day.overloaded,
                    overnight_note=day.overnight_note,
                )
            )
    await session.flush()

    driven = segment_mode_for(route.base_mode) == "car"
    leg_minutes = _leg_minutes(stop_rows, segments, driven)
    synthetic = routing.get("synthetic") is not False
    shown = route_difficulty(
        _difficulty_days(route, stop_rows, segments, days, leg_minutes), synthetic=synthetic
    )
    reward = route_difficulty(
        _difficulty_days(route, stop_rows, segments, automatic, leg_minutes), synthetic=synthetic
    )
    apply_difficulty(route, shown, reward)
    day_rows = (
        await session.scalars(
            select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
        )
    ).all()
    for day_row, scored in zip(day_rows, shown.days, strict=False):
        if day_row.difficulty_level != scored.level:
            day_row.difficulty_level = scored.level
    await session.flush()
    return RouteStructure(
        segments=segments,
        days=days,
        auto_day_count=len(automatic),
        difficulty=shown,
        reward_difficulty=reward,
    )


def apply_difficulty(route: Route, shown: RouteDifficulty, reward: RouteDifficulty) -> None:
    """Store the estimates and what people see; writes only what changed.

    A manual rating stays; the author's is kept within one step below the
    estimate, the editors' and a rating from before spec 17 show as they are
    (D9, D13, section 9).
    """
    level = shown_level(
        shown.level,
        route.difficulty_manual,
        editorial=route.difficulty_manual_by in ("editorial", "legacy"),
    )
    values: dict[str, Any] = {
        "difficulty_auto": shown.level,
        "difficulty_reward": reward.level,
        "difficulty_confidence": shown.confidence,
        "difficulty_formula_version": shown.formula_version,
        "difficulty_level": level,
        "difficulty": legacy_name(level),
    }
    for name, value in values.items():
        if getattr(route, name) != value:
            setattr(route, name, value)
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    meta = shown.as_meta()
    if accessibility.get("difficulty") != meta:
        route.accessibility = {**accessibility, "difficulty": meta}


def _difficulty_days(
    route: Route,
    stop_rows: Sequence[Any],
    segments: Sequence[PlannedSegment],
    days: Sequence[PlannedDay],
    leg_minutes: Sequence[int],
) -> list[DayInput]:
    """Each day's segments and hours for the estimate (spec 17, section 2).

    Leg i joins stop i to stop i + 1; the leg into a day's first stop is
    that day's morning, so it belongs to the day it leads into.
    """
    if not stop_rows:
        return []
    index_of = {row[0]: index for index, row in enumerate(stop_rows)}
    terrain = _terrain_by_segment(route)
    routing = _routing(route)
    # One number for the whole line until segments carry their own (17-3):
    # only trusted for walking when nothing on the route is driven.
    route_slope = routing.get("max_road_angle_degrees")
    walk_only = all(segment.mode != "car" for segment in segments)
    # A walking route keeps its climb for the whole line only; share it out
    # by length so each day gets its part (spec 17, section 2).
    shared_climb: dict[tuple[int, int], tuple[int, int]] = {}
    route_gain, route_loss = (
        routing.get("elevation_gain_meters"),
        routing.get("elevation_loss_meters"),
    )
    total_walk = sum(s.distance_meters or 0 for s in segments if s.mode == "walk")
    if (
        walk_only
        and total_walk > 0
        and isinstance(route_gain, int)
        and all(s.elevation_gain_meters is None for s in segments)
    ):
        loss = route_loss if isinstance(route_loss, int) else route_gain
        for s in segments:
            share = (s.distance_meters or 0) / total_walk
            shared_climb[(s.leg_index, s.seq)] = (round(route_gain * share), round(loss * share))
    inputs: list[DayInput] = []
    last_index = max(0, len(stop_rows) - 1)
    spans = [
        (index_of.get(day.first_stop_id, 0), index_of.get(day.last_stop_id, last_index))
        for day in days
    ] or [(0, last_index)]
    for first, last in spans:
        legs = range(max(0, first - 1), last)
        day_segments = [s for s in segments if s.leg_index in legs]
        visits = sum(
            max(5, stop_rows[i][2] or _DEFAULT_VISIT_MINUTES) for i in range(first, last + 1)
        )
        inputs.append(
            DayInput(
                segments=[
                    SegmentInput(
                        mode=segment.mode,
                        distance_meters=segment.distance_meters,
                        duration_seconds=segment.duration_seconds,
                        ascent_meters=(
                            segment.elevation_gain_meters
                            if segment.elevation_gain_meters is not None
                            else shared_climb.get((segment.leg_index, segment.seq), (None, None))[0]
                        ),
                        descent_meters=(
                            segment.elevation_loss_meters
                            if segment.elevation_loss_meters is not None
                            else shared_climb.get((segment.leg_index, segment.seq), (None, None))[1]
                        ),
                        max_slope_degrees=(
                            float(route_slope)
                            if walk_only and isinstance(route_slope, (int, float))
                            else None
                        ),
                        terrain=terrain.get((segment.leg_index, segment.seq), {}),
                        terrain_known=(segment.leg_index, segment.seq) in terrain,
                    )
                    for segment in day_segments
                ],
                total_minutes=sum(leg_minutes[i] for i in legs if i < len(leg_minutes)) + visits,
            )
        )
    return inputs


def _terrain_by_segment(route: Route) -> dict[tuple[int, int], dict[str, int]]:
    """Metres by ground category per segment, once fetched (spec 17, 3)."""
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    raw = accessibility.get("terrain")
    if not isinstance(raw, dict):
        return {}
    found: dict[tuple[int, int], dict[str, int]] = {}
    for item in raw.get("segments") or []:
        if not isinstance(item, dict):
            continue
        leg, seq, meters = item.get("leg_index"), item.get("seq"), item.get("meters")
        if isinstance(leg, int) and isinstance(seq, int) and isinstance(meters, dict):
            found[(leg, seq)] = {
                str(key): int(value)
                for key, value in meters.items()
                if isinstance(value, (int, float))
            }
    return found


def _auto_days(
    route: Route,
    stop_rows: Sequence[Any],
    segments: Sequence[PlannedSegment],
) -> list[PlannedDay]:
    """Days by the route's norms and daylight (spec 14a, section 1)."""
    if not stop_rows:
        return []
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    first_lng, first_lat = stop_rows[0][4], stop_rows[0][5]
    driven = segment_mode_for(route.base_mode) == "car"
    norm = day_norm_minutes(
        pace=accessibility.get("travel_pace")
        if isinstance(accessibility.get("travel_pace"), str)
        else None,
        driven=driven,
        with_children=accessibility.get("with_children") is True,
        lat=float(first_lat) if first_lat is not None else None,
        lng=float(first_lng) if first_lng is not None else None,
        seasons=route.seasonality,
    )
    stops = [
        DayStop(
            stop_id=row[0],
            name=row[1],
            visit_minutes=max(5, row[2] or _DEFAULT_VISIT_MINUTES),
            time_of_day=row[3] or "any",
        )
        for row in stop_rows
    ]
    return [
        PlannedDay(
            day_index=day.day_index,
            first_stop_id=day.first_stop_id,
            last_stop_id=day.last_stop_id,
            overloaded=day.overloaded,
            overnight_note=day.overnight_note,
        )
        for day in split_days(stops, _leg_minutes(stop_rows, segments, driven), norm_minutes=norm)
    ]


def _leg_minutes(
    stop_rows: Sequence[Any], segments: Sequence[PlannedSegment], driven: bool
) -> list[int]:
    """Minutes of each leg: its segments' times, else a straight-line guess."""
    by_leg: dict[int, int] = {}
    known: set[int] = set()
    for segment in segments:
        if segment.duration_seconds is not None:
            by_leg[segment.leg_index] = by_leg.get(segment.leg_index, 0) + segment.duration_seconds
            known.add(segment.leg_index)
    speed = _DRIVE_MPS if driven else _WALK_MPS
    minutes: list[int] = []
    for index in range(len(stop_rows) - 1):
        if index in known:
            minutes.append(math.ceil(by_leg[index] / 60))
            continue
        a, b = stop_rows[index], stop_rows[index + 1]
        if None in (a[4], a[5], b[4], b[5]):
            minutes.append(0)
            continue
        meters = _haversine(float(a[4]), float(a[5]), float(b[4]), float(b[5])) * 1.3
        minutes.append(math.ceil(meters / speed / 60))
    return minutes


def _haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))
