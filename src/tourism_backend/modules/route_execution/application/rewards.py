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

_WALK_MODES = frozenset({"walk", "walking", "pedestrian", "foot"})
_DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "easy": 1.0,
    "лёгкий": 1.0,
    "легкий": 1.0,
    "moderate": 1.25,
    "средний": 1.25,
    "hard": 1.5,
    "сложный": 1.5,
    "difficult": 1.5,
}


@dataclass(frozen=True, slots=True)
class RouteEffort:
    """What the traveller actually did, projected from the start snapshot."""

    completed_required_stops: int
    distance_meters: int | None = None
    elevation_gain_meters: int | None = None
    max_road_angle_degrees: float | None = None
    transport_mode: str | None = None
    difficulty: str | None = None


def difficulty_multiplier(difficulty: str | None) -> float:
    if not difficulty:
        return 1.0
    return _DIFFICULTY_MULTIPLIERS.get(difficulty.casefold().strip(), 1.0)


def travel_points_for_effort(effort: RouteEffort) -> int:
    """Points for one completed execution. Never negative, always bounded."""

    stops = max(0, effort.completed_required_stops) * POINTS_PER_STOP

    distance_km = max(0, effort.distance_meters or 0) / 1000
    is_walk = (effort.transport_mode or "").casefold().strip() in _WALK_MODES
    per_km = POINTS_PER_WALK_KM if is_walk else POINTS_PER_DRIVE_KM
    distance = distance_km * per_km

    elevation = max(0, effort.elevation_gain_meters or 0) / METERS_PER_ELEVATION_POINT

    angle = effort.max_road_angle_degrees or 0.0
    slope_bonus = STEEP_SLOPE_BONUS if angle > STEEP_SLOPE_DEGREES else 0

    raw = (BASE_POINTS + stops + distance + elevation + slope_bonus) * difficulty_multiplier(
        effort.difficulty
    )
    return max(0, min(MAX_POINTS, round(raw)))
