"""Travel points for a completed route.

A flat per-route reward would pay the same for a seaside stroll and a
mountain day with the same number of stops, so the amount is derived from
what the route actually demanded: how far it went, how much it climbed, how
steep it got, and how many stops the traveller really marked.

All inputs come from the route's **immutable routing snapshot**, captured
when the execution started (see ``routing_snapshot.py``). The route cannot be
edited afterwards to inflate a reward, and only stops the user actually
completed are counted, so an instant start-then-finish earns the base amount
rather than the full route.

A snapshot taken after spec 14 carries the route's segments: walking is paid
by the kilometre and the climb, driving at a lower rate and without the
climb, and hand-set transit legs not at all until 12b routes them. The caps
grow with the route's days. Older snapshots have no segments and keep the
rules they started with (spec 14, D18).
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_POINTS = 10
POINTS_PER_STOP = 3
POINTS_PER_WALK_KM = 1.0
POINTS_PER_DRIVE_KM = 0.2
METERS_PER_ELEVATION_POINT = 20
STEEP_SLOPE_DEGREES = 20.0
STEEP_SLOPE_BONUS = 15
MAX_POINTS = 300
# Per day of the route: a long drive must not outweigh the walking (D20).
MAX_DRIVE_POINTS_PER_DAY = 60

_WALK_MODES = frozenset({"walk", "walking", "pedestrian", "foot"})
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "easy": 1.0,
    "лёгкий": 1.0,
    "легкий": 1.0,
    "moderate": 1.25,
    "средний": 1.25,
    "hard": 1.5,
    "extreme": 1.5,
    "сложный": 1.5,
    "difficult": 1.5,
}


@dataclass(frozen=True, slots=True)
class SegmentEffort:
    """One segment of the start snapshot."""

    mode: str
    role: str
    distance_meters: int | None
    elevation_gain_meters: int | None = None


@dataclass(frozen=True, slots=True)
class RouteEffort:
    """What the traveller actually did, projected from the start snapshot."""

    completed_required_stops: int
    distance_meters: int | None = None
    elevation_gain_meters: int | None = None
    max_road_angle_degrees: float | None = None
    transport_mode: str | None = None
    difficulty: str | None = None
    segments: tuple[SegmentEffort, ...] = ()
    day_count: int = 1


def difficulty_multiplier(difficulty: str | None) -> float:
    if not difficulty:
        return 1.0
    return _DIFFICULTY_MULTIPLIERS.get(difficulty.casefold().strip(), 1.0)


def _legacy_distance_and_climb(effort: RouteEffort) -> tuple[float, float, bool]:
    distance_km = max(0, effort.distance_meters or 0) / 1000
    is_walk = (effort.transport_mode or "").casefold().strip() in _WALK_MODES
    per_km = POINTS_PER_WALK_KM if is_walk else POINTS_PER_DRIVE_KM
    elevation = max(0, effort.elevation_gain_meters or 0) / METERS_PER_ELEVATION_POINT
    return distance_km * per_km, elevation, True


def _segment_distance_and_climb(effort: RouteEffort, days: int) -> tuple[float, float, bool]:
    """Distance and climb points from segments; the flag says walking was involved.

    The way back to the car (``return``) is walked but not paid (D22). A
    route whose legs have no router numbers falls back to its total length
    in the mode of its segments.
    """

    paid = [s for s in effort.segments if s.role != "return"]
    walk = [s for s in paid if s.mode == "walk"]
    car = [s for s in paid if s.mode == "car"]
    if any(s.distance_meters is None for s in walk + car):
        # Without per-leg numbers a walk-and-drive route counts as driven:
        # the lower rate is the safe guess. Transit alone is not paid.
        total_km = max(0, effort.distance_meters or 0) / 1000
        walk_km = total_km if walk and not car else 0.0
        car_km = total_km if car else 0.0
    else:
        walk_km = sum(s.distance_meters or 0 for s in walk) / 1000
        car_km = sum(s.distance_meters or 0 for s in car) / 1000
    drive = min(car_km * POINTS_PER_DRIVE_KM, MAX_DRIVE_POINTS_PER_DAY * days)

    if any(s.elevation_gain_meters is not None for s in walk):
        climb_m = sum(s.elevation_gain_meters or 0 for s in walk)
    elif walk and not car:
        # Segments have no climb of their own yet: an all-walking route
        # climbed what the whole route climbs.
        climb_m = effort.elevation_gain_meters or 0
    else:
        climb_m = 0
    elevation = max(0, climb_m) / METERS_PER_ELEVATION_POINT
    return walk_km * POINTS_PER_WALK_KM + drive, elevation, bool(walk)


def travel_points_for_effort(effort: RouteEffort) -> int:
    """Points for one completed execution. Never negative, always bounded."""

    days = max(1, effort.day_count)
    stops = max(0, effort.completed_required_stops) * POINTS_PER_STOP
    if effort.segments:
        distance, elevation, walked = _segment_distance_and_climb(effort, days)
    else:
        distance, elevation, walked = _legacy_distance_and_climb(effort)

    angle = effort.max_road_angle_degrees or 0.0
    slope_bonus = STEEP_SLOPE_BONUS if walked and angle > STEEP_SLOPE_DEGREES else 0

    raw = (BASE_POINTS + stops + distance + elevation + slope_bonus) * difficulty_multiplier(
        effort.difficulty
    )
    return max(0, min(MAX_POINTS * days, round(raw)))
