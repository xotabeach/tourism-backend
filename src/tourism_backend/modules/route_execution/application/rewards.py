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

from collections.abc import Sequence
from dataclasses import dataclass

from tourism_backend.modules.routes.application.difficulty import reward_multiplier

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
    # Spec 17: the estimate at the run start, by the days the norms give.
    # Runs started before it have none and are paid by the word above.
    difficulty_level: int | None = None
    segments: tuple[SegmentEffort, ...] = ()
    day_count: int = 1
    # How much of the route's way was really walked, 0..1 (spec 15, D14).
    # Below 1 the difficulty multiplier shrinks with it and the steep slope
    # bonus is not paid: skipping the hard part must not keep its premium.
    paid_share: float = 1.0


#: Skip reasons after which the way to the stop was still walked: the walker
#: got there and found it shut (spec 15, D14).
SKIP_REASONS_PAYING_THE_LEG = frozenset({"closed"})


@dataclass(frozen=True, slots=True)
class StopState:
    """What happened to one stop of a run, for deciding which legs to pay."""

    position: int
    is_optional: bool
    marked: bool
    skip_reason: str | None = None


def paid_leg_positions(stops: Sequence[StopState], *, run_completed: bool) -> set[int]:
    """Positions of the stops whose incoming leg was walked.

    A marked stop was reached, and so was one skipped as «closed». A stop
    skipped for any other reason was not gone to. An optional stop nobody
    touched does not break the way: it is paid when the walker went past it,
    which is always in a completed run and only up to the last reached stop
    in one ended early.
    """

    reached = {
        stop.position
        for stop in stops
        if stop.marked or stop.skip_reason in SKIP_REASONS_PAYING_THE_LEG
    }
    last_reached = max(reached, default=0)
    passed_by = {
        stop.position
        for stop in stops
        if stop.is_optional
        and not stop.marked
        and stop.skip_reason is None
        and (run_completed or stop.position < last_reached)
    }
    return reached | passed_by


def paid_way_share(
    segments: Sequence[tuple[SegmentEffort, bool]],
    legs: Sequence[tuple[int | None, bool]] = (),
) -> float:
    """The walked part of the route's way, 0..1.

    ``segments`` pairs every snapshot segment with whether its leg is paid;
    ``legs`` does the same for the length of every leg as the run recorded
    it at the start. Segment distances are the most exact; a snapshot that
    has none falls back to the leg lengths, and legs without a length to
    their plain count.
    """

    countable = [
        (segment, paid)
        for segment, paid in segments
        if segment.role != "return" and segment.mode in {"walk", "car"}
    ]
    if countable and all(segment.distance_meters for segment, _paid in countable):
        total = sum(segment.distance_meters or 0 for segment, _paid in countable)
        walked = sum(segment.distance_meters or 0 for segment, paid in countable if paid)
        return walked / total
    if not legs:
        return 1.0
    if all(meters for meters, _paid in legs):
        total = sum(meters or 0 for meters, _paid in legs)
        return sum(meters or 0 for meters, paid in legs if paid) / total
    return sum(1 for _meters, paid in legs if paid) / len(legs)


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

    share = min(1.0, max(0.0, effort.paid_share))
    angle = effort.max_road_angle_degrees or 0.0
    # The snapshot knows the steepest slope of the route, not where it is:
    # with a part of the way skipped it cannot be shown to have been walked.
    steep = walked and angle > STEEP_SLOPE_DEGREES and share >= 1.0
    slope_bonus = STEEP_SLOPE_BONUS if steep else 0

    multiplier = (
        reward_multiplier(effort.difficulty_level)
        if effort.difficulty_level is not None
        else difficulty_multiplier(effort.difficulty)
    )
    multiplier = 1.0 + (multiplier - 1.0) * share
    raw = (BASE_POINTS + stops + distance + elevation + slope_bonus) * multiplier
    return max(0, min(MAX_POINTS * days, round(raw)))
