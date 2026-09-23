"""Drive to a car park, walk the rest (spec 14b, section 2).

A driven route used to end at the nearest street, so a mountain place a
car cannot reach got a straight line or a forest track. Here every stop gets
an access point: the stop itself when a car stops within 300 m of it, else
the car park with the shortest walk to it (D6). A leg is then the walk back
to the car, the drive, and the walk up to the next stop; each part is routed
on its own, so one failure does not flatten the whole route (R3).

The walk to the first stop and from the last one are not part of the route:
it starts and ends at its stops.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from tourism_backend.modules.route_builder.application.polyline import encode_polyline6
from tourism_backend.modules.route_builder.application.routing import (
    RouteLegResult,
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
)

# A car that stops this close to a stop needs no separate walk (D6).
APPROACH_MIN_METERS = 300
# Straight-line speeds for parts the router could not build.
_WALK_MPS = 1.25
_CAR_MPS = 11.1
_Point = tuple[float, float]  # (lng, lat)


class SegmentRouter(Protocol):
    async def route(
        self, *, waypoints: list[RouteWaypoint], transport_mode: TransportMode
    ) -> RoutingResult: ...

    async def locate_car(self, point: RouteWaypoint) -> _Point | None: ...

    async def walk_distances(
        self, sources: Sequence[RouteWaypoint], target: RouteWaypoint
    ) -> list[int | None]: ...


ParkingLookup = Callable[[RouteWaypoint], Awaitable[Sequence[_Point]]]


@dataclass(frozen=True, slots=True)
class BuiltSegment:
    leg_index: int
    seq: int
    mode: str
    role: str
    origin: str
    distance_meters: int
    duration_seconds: int
    points: tuple[_Point, ...]
    elevation_gain_meters: int | None = None
    elevation_loss_meters: int | None = None

    def as_meta(self) -> dict[str, object]:
        """The form kept in ``routes.accessibility.routing.segments``."""
        return {
            "leg_index": self.leg_index,
            "seq": self.seq,
            "mode": self.mode,
            "role": self.role,
            "origin": self.origin,
            "distance_meters": self.distance_meters,
            "duration_seconds": self.duration_seconds,
            "elevation_gain_meters": self.elevation_gain_meters,
            "elevation_loss_meters": self.elevation_loss_meters,
            "shape": encode_polyline6(self.points),
        }


@dataclass(frozen=True, slots=True)
class _Access:
    """Where the car stops for a stop, and the walk from there if any."""

    point: _Point
    walk: BuiltSegment | None = None
    needs_review: bool = False


@dataclass(slots=True)
class MixedRoute:
    segments: list[BuiltSegment]
    result: RoutingResult
    warnings: list[str] = field(default_factory=list)


def _meters(a: _Point, b: _Point) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def _waypoint(point: _Point) -> RouteWaypoint:
    return RouteWaypoint(lng=point[0], lat=point[1])


def _points(result: RoutingResult) -> tuple[_Point, ...]:
    wkt = result.geometry_wkt or ""
    inner = wkt[wkt.find("(") + 1 : wkt.rfind(")")]
    pairs = []
    for raw in inner.split(","):
        values = raw.split()
        if len(values) >= 2:
            pairs.append((float(values[0]), float(values[1])))
    return tuple(pairs)


def _straight(
    a: _Point, b: _Point, *, leg_index: int, seq: int, mode: str, role: str
) -> BuiltSegment:
    meters = round(_meters(a, b))
    speed = _WALK_MPS if mode == "walk" else _CAR_MPS
    return BuiltSegment(
        leg_index=leg_index,
        seq=seq,
        mode=mode,
        role=role,
        origin="synthetic",
        distance_meters=meters,
        duration_seconds=round(meters / speed),
        points=(a, b),
    )


async def _routed(
    router: SegmentRouter,
    a: _Point,
    b: _Point,
    *,
    mode: TransportMode,
    role: str,
) -> BuiltSegment | None:
    try:
        result = await router.route(waypoints=[_waypoint(a), _waypoint(b)], transport_mode=mode)
    except RoutingError:
        return None
    points = _points(result) or (a, b)
    return BuiltSegment(
        leg_index=0,
        seq=0,
        mode=mode,
        role=role,
        origin="router",
        distance_meters=result.total_distance_meters,
        duration_seconds=result.total_duration_seconds,
        points=points,
        elevation_gain_meters=result.elevation_gain_meters if mode == "walk" else None,
        elevation_loss_meters=result.elevation_loss_meters if mode == "walk" else None,
    )


async def _access(router: SegmentRouter, parkings: ParkingLookup, stop: RouteWaypoint) -> _Access:
    here = (stop.lng, stop.lat)
    try:
        snapped = await router.locate_car(stop)
    except RoutingError:
        snapped = None
    if snapped is not None and _meters(snapped, here) <= APPROACH_MIN_METERS:
        return _Access(point=here)
    candidates = list(await parkings(stop))
    if snapped is not None:
        # The street end of a drive is the fallback car park (D6).
        candidates.append(snapped)
    if not candidates:
        return _Access(point=here, needs_review=True)
    try:
        walks = await router.walk_distances([_waypoint(c) for c in candidates], stop)
    except RoutingError:
        walks = [None] * len(candidates)
    reachable = [(w, c) for w, c in zip(walks, candidates, strict=False) if w is not None]
    if not reachable:
        start = snapped or candidates[0]
        guess = _straight(start, here, leg_index=0, seq=0, mode="walk", role="approach")
        return _Access(point=start, walk=guess, needs_review=True)
    distance, parking = min(reachable)
    if distance < APPROACH_MIN_METERS:
        return _Access(point=parking)
    walk = await _routed(router, parking, here, mode="walk", role="approach")
    if walk is None:
        walk = _straight(parking, here, leg_index=0, seq=0, mode="walk", role="approach")
        return _Access(point=parking, walk=walk, needs_review=True)
    return _Access(point=parking, walk=walk)


def _placed(segment: BuiltSegment, leg_index: int, seq: int, **changes: object) -> BuiltSegment:
    values = {slot: getattr(segment, slot) for slot in BuiltSegment.__slots__}
    values.update(leg_index=leg_index, seq=seq, **changes)
    return BuiltSegment(**values)


async def build_driven_route(
    stops: Sequence[RouteWaypoint],
    *,
    router: SegmentRouter,
    parkings: ParkingLookup,
) -> MixedRoute:
    """Segments and the summed routing result for a car or mixed route."""
    if len(stops) < 2:
        raise RoutingError("routing_provider_error", "At least two waypoints are required")
    accesses = await asyncio.gather(*(_access(router, parkings, stop) for stop in stops))
    warnings: list[str] = []
    for stop, access in zip(stops, accesses, strict=True):
        if access.needs_review:
            warnings.append(f"approach_needs_review:{stop.place_id or ''}")

    async def leg(index: int) -> list[BuiltSegment]:
        here, there = accesses[index], accesses[index + 1]
        a, b = stops[index], stops[index + 1]
        if here.point == there.point:
            # Both stops hang off the same car park: walk between them.
            walked = await _routed(router, (a.lng, a.lat), (b.lng, b.lat), mode="walk", role="main")
            return [
                walked
                or _straight(
                    (a.lng, a.lat), (b.lng, b.lat), leg_index=0, seq=0, mode="walk", role="main"
                )
            ]
        parts: list[BuiltSegment] = []
        if here.walk is not None:
            back = here.walk
            parts.append(
                _placed(
                    back,
                    0,
                    0,
                    role="return",
                    points=tuple(reversed(back.points)),
                    elevation_gain_meters=back.elevation_loss_meters,
                    elevation_loss_meters=back.elevation_gain_meters,
                )
            )
        drive = await _routed(router, here.point, there.point, mode="car", role="main")
        parts.append(
            drive or _straight(here.point, there.point, leg_index=0, seq=0, mode="car", role="main")
        )
        if there.walk is not None:
            parts.append(there.walk)
        return parts

    legs = await asyncio.gather(*(leg(i) for i in range(len(stops) - 1)))
    segments = [
        _placed(segment, leg_index, seq)
        for leg_index, parts in enumerate(legs)
        for seq, segment in enumerate(parts)
    ]
    return MixedRoute(segments=segments, result=_summary(segments, legs), warnings=warnings)


def _summary(segments: list[BuiltSegment], legs: Sequence[Sequence[BuiltSegment]]) -> RoutingResult:
    line: list[_Point] = []
    for segment in segments:
        points = list(segment.points)
        line.extend(points[1:] if line and points and points[0] == line[-1] else points)
    leg_results = tuple(
        RouteLegResult(
            from_index=index,
            to_index=index + 1,
            distance_meters=sum(s.distance_meters for s in parts),
            duration_seconds=sum(s.duration_seconds for s in parts),
            geometry_wkt=None,
        )
        for index, parts in enumerate(legs)
    )
    walked = [s for s in segments if s.mode == "walk"]
    gains = [s.elevation_gain_meters for s in walked if s.elevation_gain_meters is not None]
    losses = [s.elevation_loss_meters for s in walked if s.elevation_loss_meters is not None]
    wkt = (
        "LINESTRING(" + ", ".join(f"{lng:.6f} {lat:.6f}" for lng, lat in line) + ")"
        if len(line) >= 2
        else None
    )
    return RoutingResult(
        provider="valhalla",
        synthetic=all(s.origin == "synthetic" for s in segments),
        legs=leg_results,
        total_distance_meters=sum(leg.distance_meters for leg in leg_results),
        total_duration_seconds=sum(leg.duration_seconds for leg in leg_results),
        geometry_wkt=wkt,
        elevation_gain_meters=sum(gains) if gains else None,
        elevation_loss_meters=sum(losses) if losses else None,
    )
