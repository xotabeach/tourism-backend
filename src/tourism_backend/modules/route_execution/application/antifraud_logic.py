"""Pure anti-fraud rules for route completion (no database, no clock).

Everything here is deterministic so the rules can be unit-tested in isolation
and reused by the service layer, the admin views and the retention script.
The server is the only arbiter: the mobile client may mirror these rules to
show hints, but nothing it computes is trusted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal

#: Straight line between two stops is shorter than any real path.
STRAIGHT_LINE_DETOUR_FACTOR = 1.3

#: Moscow has been UTC+3 without DST since 2014; a fixed offset avoids a
#: dependency on the system tz database (absent in slim runtime images).
MOSCOW_TZ = timezone(timedelta(hours=3))

_EARTH_RADIUS_M = 6_371_000.0

# Deliberately optimistic speeds: an under-estimate of the leg time makes the
# "too fast" rule more forgiving, which is the safe direction for honest users.
_WALK_SPEED_MPS = 1.25  # 4.5 km/h
_BIKE_SPEED_MPS = 4.2  # 15 km/h
_CAR_SPEED_MPS = 11.1  # 40 km/h
_PUBLIC_SPEED_MPS = 6.9  # 25 km/h

_WALK_MODES = frozenset({"walk", "walking", "pedestrian", "foot", "mixed", "hiking"})
_BIKE_MODES = frozenset({"bike", "bicycle", "cycling"})
_CAR_MODES = frozenset({"car", "driving", "drive", "auto"})
_PUBLIC_MODES = frozenset({"public", "bus", "transit"})

LegSource = Literal["provider", "straight_line"]


@dataclass(frozen=True, slots=True)
class StopPoint:
    """A stop of a run, in route order."""

    position: int
    lat: float | None
    lng: float | None


@dataclass(frozen=True, slots=True)
class LegEstimate:
    """Expected distance and time from the previous stop to this one."""

    distance_meters: int
    duration_seconds: int
    source: LegSource


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def speed_mps_for(transport_mode: str | None) -> float:
    mode = (transport_mode or "").casefold().strip()
    if mode in _CAR_MODES:
        return _CAR_SPEED_MPS
    if mode in _PUBLIC_MODES:
        return _PUBLIC_SPEED_MPS
    if mode in _BIKE_MODES:
        return _BIKE_SPEED_MPS
    return _WALK_SPEED_MPS


def straight_line_leg(
    previous: StopPoint,
    current: StopPoint,
    *,
    transport_mode: str | None,
) -> LegEstimate | None:
    """Estimate one leg from coordinates alone; ``None`` when a stop has none."""

    if previous.lat is None or previous.lng is None or current.lat is None or current.lng is None:
        return None
    direct = haversine_meters(previous.lat, previous.lng, current.lat, current.lng)
    distance = direct * STRAIGHT_LINE_DETOUR_FACTOR
    duration = distance / speed_mps_for(transport_mode)
    return LegEstimate(
        distance_meters=round(distance),
        duration_seconds=round(duration),
        source="straight_line",
    )


def provider_legs_from_metadata(
    routing_meta: Mapping[str, object] | None,
    *,
    stop_count: int,
) -> list[LegEstimate] | None:
    """Read per-leg provider data if a route ever persisted it.

    Today generated routes only keep aggregate durations, so this normally
    returns ``None`` and callers fall back to the straight-line estimate. The
    expected shape is ``routing["legs"] = [{"distance_meters", "duration_seconds"}]``
    with exactly one entry between each pair of consecutive stops.
    """

    if not routing_meta:
        return None
    raw = routing_meta.get("legs")
    if not isinstance(raw, list) or len(raw) != max(0, stop_count - 1):
        return None
    legs: list[LegEstimate] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        distance = item.get("distance_meters")
        duration = item.get("duration_seconds")
        if (
            isinstance(distance, bool)
            or isinstance(duration, bool)
            or not isinstance(distance, (int, float))
            or not isinstance(duration, (int, float))
            or distance < 0
            or duration < 0
        ):
            return None
        legs.append(
            LegEstimate(
                distance_meters=round(distance),
                duration_seconds=round(duration),
                source="provider",
            )
        )
    return legs


def build_leg_estimates(
    stops: Sequence[StopPoint],
    *,
    transport_mode: str | None,
    provider_legs: Sequence[LegEstimate] | None = None,
) -> list[LegEstimate | None]:
    """One estimate per stop in order; the first stop has no leg (``None``)."""

    ordered = sorted(stops, key=lambda stop: stop.position)
    estimates: list[LegEstimate | None] = [None]
    for index in range(1, len(ordered)):
        if provider_legs is not None and len(provider_legs) == len(ordered) - 1:
            estimates.append(provider_legs[index - 1])
            continue
        estimates.append(
            straight_line_leg(ordered[index - 1], ordered[index], transport_mode=transport_mode)
        )
    return estimates[: len(ordered)]


# ---------------------------------------------------------------- pace rule


class PaceKind(StrEnum):
    OK = "ok"
    TOO_FAST = "too_fast"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PaceRules:
    ratio: float = 0.5
    min_estimate_seconds: int = 120
    min_mark_ratio: float = 0.2
    min_mark_gap_seconds: int = 10


@dataclass(frozen=True, slots=True)
class PaceEvaluation:
    kind: PaceKind
    actual_seconds: int | None
    estimate_seconds: int | None
    #: The mark came so soon after the previous one that it earns no stop points.
    below_floor: bool


def paused_overlap_seconds(
    intervals: Sequence[tuple[datetime, datetime | None]],
    start: datetime,
    end: datetime,
) -> int:
    """Seconds of ``[start, end]`` covered by pauses (open pause runs to ``end``)."""

    total = 0.0
    for paused_at, resumed_at in intervals:
        lo = max(paused_at, start)
        hi = min(resumed_at if resumed_at is not None else end, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


def actual_leg_seconds(
    *,
    previous_mark_at: datetime | None,
    this_mark_at: datetime,
    paused_seconds: int,
) -> int | None:
    """Time spent on the leg, net of pauses; ``None`` for the first mark."""

    if previous_mark_at is None:
        return None
    gross = (this_mark_at - previous_mark_at).total_seconds()
    return max(0, int(gross) - max(0, paused_seconds))


def evaluate_pace(
    *,
    estimate_seconds: int | None,
    actual_seconds: int | None,
    rules: PaceRules,
) -> PaceEvaluation:
    """Judge one stop mark against its leg estimate."""

    if actual_seconds is None:
        return PaceEvaluation(PaceKind.SKIPPED, None, estimate_seconds, below_floor=False)

    floor = float(rules.min_mark_gap_seconds)
    if estimate_seconds is not None and estimate_seconds > 0:
        floor = max(floor, rules.min_mark_ratio * estimate_seconds)
    below_floor = actual_seconds < floor

    if estimate_seconds is None or estimate_seconds < rules.min_estimate_seconds:
        return PaceEvaluation(PaceKind.SKIPPED, actual_seconds, estimate_seconds, below_floor)

    too_fast = actual_seconds < rules.ratio * estimate_seconds
    return PaceEvaluation(
        PaceKind.TOO_FAST if too_fast else PaceKind.OK,
        actual_seconds,
        estimate_seconds,
        below_floor,
    )


def pace_warn_below_seconds(
    *,
    estimate_seconds: int | None,
    is_first_mark: bool,
    rules: PaceRules,
) -> int | None:
    """The threshold the client may use for its pre-send hint; ``None`` = no hint."""

    if is_first_mark or estimate_seconds is None or estimate_seconds < rules.min_estimate_seconds:
        return None
    return math.ceil(rules.ratio * estimate_seconds)


# ---------------------------------------------------------------- GPS verdict


class GpsVerdict(StrEnum):
    AT = "at"
    BEHIND = "behind"
    AHEAD = "ahead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GpsRules:
    tolerance_meters: int = 150
    min_accuracy_meters: int = 100


@dataclass(frozen=True, slots=True)
class GpsReading:
    """A position reported with a stop mark. Never stored, only judged."""

    lat: float
    lng: float
    accuracy_m: float | None


@dataclass(frozen=True, slots=True)
class GpsEvaluation:
    verdict: GpsVerdict
    #: Distance to the marked stop rounded to 50 m; ``None`` when unknown.
    distance_bucket_m: int | None


_BUCKET_M = 50


def gps_verdict(
    reading: GpsReading | None,
    *,
    stops: Sequence[StopPoint],
    marked_position: int,
    rules: GpsRules,
) -> GpsEvaluation:
    """Compare the reported position with the stop being marked."""

    unknown = GpsEvaluation(GpsVerdict.UNKNOWN, None)
    if reading is None:
        return unknown
    if reading.accuracy_m is not None and reading.accuracy_m > rules.min_accuracy_meters:
        return unknown
    if not (-90 <= reading.lat <= 90 and -180 <= reading.lng <= 180):
        return unknown

    nearest: StopPoint | None = None
    nearest_distance = math.inf
    marked: StopPoint | None = None
    for stop in stops:
        if stop.position == marked_position:
            marked = stop
        if stop.lat is None or stop.lng is None:
            continue
        distance = haversine_meters(reading.lat, reading.lng, stop.lat, stop.lng)
        if distance < nearest_distance or (
            distance == nearest_distance
            and nearest is not None
            and stop.position < nearest.position
        ):
            nearest = stop
            nearest_distance = distance
    if nearest is None or nearest_distance > rules.tolerance_meters:
        return unknown

    bucket: int | None = None
    if marked is not None and marked.lat is not None and marked.lng is not None:
        to_marked = haversine_meters(reading.lat, reading.lng, marked.lat, marked.lng)
        bucket = int(round(to_marked / _BUCKET_M)) * _BUCKET_M

    if marked_position < nearest.position:
        return GpsEvaluation(GpsVerdict.BEHIND, bucket)
    if marked_position == nearest.position:
        return GpsEvaluation(GpsVerdict.AT, bucket)
    return GpsEvaluation(GpsVerdict.AHEAD, bucket)


# ------------------------------------------------------- violation counting


def is_batched(
    previous_counted_at: datetime | None,
    this_at: datetime,
    *,
    window_seconds: int,
) -> bool:
    """A violation within the window of the last counted one is part of its batch."""

    if previous_counted_at is None:
        return False
    return abs((this_at - previous_counted_at).total_seconds()) < window_seconds


def count_within(
    times: Sequence[datetime],
    *,
    now: datetime,
    window_hours: int,
    not_before: datetime | None,
) -> int:
    """How many counted violations fall in the trailing window (and after a reset)."""

    lower = now - timedelta(hours=window_hours)
    if not_before is not None:
        lower = max(lower, not_before)
    return sum(1 for moment in times if lower <= moment <= now)


@dataclass(frozen=True, slots=True)
class EscalationRules:
    flag_violations: int = 3
    flag_window_hours: int = 3
    block_violations: int = 6
    block_window_hours: int = 6
    block_ladder_hours: tuple[int, ...] = (1, 24, 168)
    ladder_memory_days: int = 30


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    should_flag: bool
    should_block: bool


def evaluate_escalation(
    counted_times: Sequence[datetime],
    *,
    now: datetime,
    counters_from: datetime | None,
    rules: EscalationRules,
) -> EscalationDecision:
    flagged = (
        count_within(
            counted_times,
            now=now,
            window_hours=rules.flag_window_hours,
            not_before=counters_from,
        )
        >= rules.flag_violations
    )
    blocked = (
        count_within(
            counted_times,
            now=now,
            window_hours=rules.block_window_hours,
            not_before=counters_from,
        )
        >= rules.block_violations
    )
    return EscalationDecision(should_flag=flagged or blocked, should_block=blocked)


@dataclass(frozen=True, slots=True)
class BlockStep:
    duration_hours: int
    new_ladder_level: int


def next_block_step(
    *,
    ladder_level: int,
    last_offence_at: datetime | None,
    now: datetime,
    rules: EscalationRules,
) -> BlockStep:
    """Pick the next rung: repeats within the memory window climb the ladder."""

    ladder = rules.block_ladder_hours or (1,)
    remembered = last_offence_at is not None and (now - last_offence_at) <= timedelta(
        days=rules.ladder_memory_days
    )
    level = max(0, ladder_level) if remembered else 0
    duration = ladder[min(level, len(ladder) - 1)]
    return BlockStep(duration_hours=duration, new_ladder_level=min(level + 1, len(ladder)))


# --------------------------------------------------------------- soft limits


def moscow_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """UTC bounds of the current Europe/Moscow calendar day."""

    local = now.astimezone(MOSCOW_TZ)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def apply_daily_cap(*, points: int, awarded_today: int, cap: int) -> int:
    """Award what still fits under the daily cap (partial award allowed)."""

    if points <= 0:
        return 0
    remaining = max(0, cap - max(0, awarded_today))
    return min(points, remaining)


def cooldown_active(
    *,
    last_awarded_completed_at: datetime | None,
    now: datetime,
    cooldown_days: int,
) -> bool:
    """True when this route already paid out inside the cooldown window."""

    if cooldown_days <= 0 or last_awarded_completed_at is None:
        return False
    return (now - last_awarded_completed_at) < timedelta(days=cooldown_days)
