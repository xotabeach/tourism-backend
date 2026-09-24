"""Route difficulty 1..5 from load and terrain, as pure rules (spec 17).

A day's walking part is scored by effort kilometres (km + ascent / 100 m +
descent / 200 m) and by the hardest terrain met for at least 200 m; its
driving part by hours behind the wheel, unpaved road and serpentines. The
day takes the harder of the two, one step more when both are demanding. The
route takes its hardest day, one step more for three days or longer of
demanding days (D3, D4, D7, D8).

Nothing here reads the database or the network: the caller hands in the
segments of each day with whatever terrain is known, as ``day_split`` does
for the day norms. Reasons come back as codes with numbers; the app words
them (spec 17, section 8).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

# Bumped whenever a threshold or rule below changes, so stored results can
# be recomputed in bulk (D13).
FORMULA_VERSION = 1

MIN_LEVEL = 1
MAX_LEVEL = 5

# Upper bounds of effort km for levels 1..4; above the last is level 5 (D7).
EFFORT_KM_LIMITS: tuple[float, ...] = (6.0, 12.0, 20.0, 28.0)
ASCENT_METERS_PER_KM = 100.0
DESCENT_METERS_PER_KM = 200.0

# Terrain counts only when there is this much of it (a short stair or rocky
# step does not make a walk hard).
TERRAIN_MIN_METERS = 200
# OSM sac_scale grades as Valhalla reports them, by the level they set.
TRAIL_GRADE_LEVELS: dict[str, int] = {"T1": 2, "T2": 3, "T3": 4, "T4": 5, "T5": 5, "T6": 5}
STEEP_SLOPE_DEGREES = 25.0

# Driving (D2, D7).
DRIVE_EASY_HOURS = 3.0
DRIVE_LONG_HOURS = 5.0
SERPENTINE_MIN_METERS = 2_000
UNPAVED_LONG_METERS = 5_000

# A day on its feet longer than this is at least level 2, visits included (D8).
LONG_DAY_MINUTES = 9 * 60
LONG_DAY_LEVEL = 2
# Three or more days add one step, only when the hardest day is demanding (D4).
MULTI_DAY_COUNT = 3
DEMANDING_LEVEL = 3

Confidence = Literal["high", "low"]
SegmentMode = str


@dataclass(frozen=True, slots=True)
class SegmentInput:
    """One segment of a day with what is known about its ground.

    ``terrain`` holds metres by category: ``T1``..``T6`` for trail grades,
    ``unpaved``, ``serpentine`` and ``offroad`` for roads. ``terrain_known``
    is false when no tags were fetched for it (not «no hard ground»).
    """

    mode: SegmentMode
    distance_meters: int | None
    duration_seconds: int | None = None
    ascent_meters: int | None = None
    descent_meters: int | None = None
    max_slope_degrees: float | None = None
    terrain: Mapping[str, int] = field(default_factory=dict)
    terrain_known: bool = False


@dataclass(frozen=True, slots=True)
class DayInput:
    segments: Sequence[SegmentInput]
    # Movement and visits together; None when unknown.
    total_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class Reason:
    """Why a level came out as it did: a code and its numbers (section 8)."""

    code: str
    values: Mapping[str, float | int | str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, **self.values}


@dataclass(frozen=True, slots=True)
class DayDifficulty:
    level: int
    walk_level: int | None
    drive_level: int | None
    effort_km: float
    reasons: tuple[Reason, ...]
    confident: bool


@dataclass(frozen=True, slots=True)
class RouteDifficulty:
    level: int
    walk_level: int | None
    drive_level: int | None
    days: tuple[DayDifficulty, ...]
    confidence: Confidence
    reasons: tuple[Reason, ...]
    formula_version: int = FORMULA_VERSION

    def as_meta(self) -> dict[str, object]:
        """The breakdown kept with the route and sent to the app."""
        return {
            "level": self.level,
            "walk_level": self.walk_level,
            "drive_level": self.drive_level,
            "confidence": self.confidence,
            "formula_version": self.formula_version,
            "reasons": [reason.as_dict() for reason in self.reasons],
            "days": [
                {
                    "level": day.level,
                    "walk_level": day.walk_level,
                    "drive_level": day.drive_level,
                    "effort_km": day.effort_km,
                    "reasons": [reason.as_dict() for reason in day.reasons],
                }
                for day in self.days
            ],
        }


def _clamp(level: int) -> int:
    return max(MIN_LEVEL, min(MAX_LEVEL, level))


def effort_level(effort_km: float) -> int:
    for index, limit in enumerate(EFFORT_KM_LIMITS):
        if effort_km <= limit:
            return index + 1
    return MAX_LEVEL


def _walk_part(
    segments: Sequence[SegmentInput],
) -> tuple[int, float, list[Reason], bool]:
    km = sum((s.distance_meters or 0) for s in segments) / 1000
    ascent = sum((s.ascent_meters or 0) for s in segments)
    descent = sum((s.descent_meters or 0) for s in segments)
    effort = round(km + ascent / ASCENT_METERS_PER_KM + descent / DESCENT_METERS_PER_KM, 1)
    by_effort = effort_level(effort)
    reasons = [
        Reason(
            "walk_effort",
            {"effort_km": effort, "km": round(km, 1), "ascent_m": ascent, "descent_m": descent},
        )
    ]

    by_terrain = MIN_LEVEL
    grades: dict[str, int] = {}
    for segment in segments:
        for grade, meters in segment.terrain.items():
            if grade in TRAIL_GRADE_LEVELS:
                grades[grade] = grades.get(grade, 0) + meters
    hardest: tuple[str, int] | None = None
    for grade, meters in grades.items():
        level = TRAIL_GRADE_LEVELS[grade]
        if meters >= TERRAIN_MIN_METERS and level > by_terrain:
            by_terrain, hardest = level, (grade, meters)
    if hardest is not None:
        reasons.append(Reason("trail", {"grade": hardest[0], "meters": hardest[1]}))
    steepest = max((s.max_slope_degrees or 0.0 for s in segments), default=0.0)
    if steepest > STEEP_SLOPE_DEGREES and by_terrain < 4:
        by_terrain = 4
        reasons.append(Reason("steep", {"degrees": round(steepest)}))

    long_enough = [s for s in segments if (s.distance_meters or 0) >= TERRAIN_MIN_METERS]
    confident = all(s.terrain_known and s.ascent_meters is not None for s in long_enough)
    level = max(by_effort, by_terrain)
    # The effort line leads when it decided the level, the terrain otherwise.
    if by_terrain > by_effort:
        reasons.append(reasons.pop(0))
    return level, effort, reasons, confident


def _drive_part(segments: Sequence[SegmentInput]) -> tuple[int, list[Reason], bool]:
    hours = sum((s.duration_seconds or 0) for s in segments) / 3600
    unpaved = sum(s.terrain.get("unpaved", 0) for s in segments)
    serpentine = sum(s.terrain.get("serpentine", 0) for s in segments)
    offroad = sum(s.terrain.get("offroad", 0) for s in segments)
    reasons: list[Reason] = []
    level = MIN_LEVEL
    if offroad >= TERRAIN_MIN_METERS:
        level = 5
        reasons.append(Reason("offroad", {"meters": offroad}))
    elif unpaved > UNPAVED_LONG_METERS:
        level = 4
        reasons.append(Reason("unpaved", {"meters": unpaved}))
    elif unpaved >= TERRAIN_MIN_METERS or hours > DRIVE_LONG_HOURS:
        level = 3
        if unpaved >= TERRAIN_MIN_METERS:
            reasons.append(Reason("unpaved", {"meters": unpaved}))
    elif serpentine >= SERPENTINE_MIN_METERS or hours > DRIVE_EASY_HOURS:
        level = 2
        if serpentine >= SERPENTINE_MIN_METERS:
            reasons.append(Reason("serpentine", {"meters": serpentine}))
    reasons.insert(0, Reason("drive_hours", {"hours": round(hours, 1)}))
    confident = all(s.terrain_known for s in segments)
    return level, reasons, confident


def day_difficulty(day: DayInput) -> DayDifficulty:
    walked = [s for s in day.segments if s.mode == "walk"]
    driven = [s for s in day.segments if s.mode == "car"]
    walk_level: int | None = None
    drive_level: int | None = None
    effort = 0.0
    reasons: list[Reason] = []
    confident = True
    if walked:
        walk_level, effort, walk_reasons, walk_sure = _walk_part(walked)
        reasons.extend(walk_reasons)
        confident = confident and walk_sure
    if driven:
        drive_level, drive_reasons, drive_sure = _drive_part(driven)
        reasons.extend(drive_reasons)
        confident = confident and drive_sure
    level = max(walk_level or MIN_LEVEL, drive_level or MIN_LEVEL)
    if (
        walk_level is not None
        and drive_level is not None
        and walk_level >= DEMANDING_LEVEL
        and drive_level >= DEMANDING_LEVEL
    ):
        level += 1
        reasons.append(Reason("walk_and_drive"))
    if day.total_minutes is not None and day.total_minutes > LONG_DAY_MINUTES:
        if level < LONG_DAY_LEVEL:
            level = LONG_DAY_LEVEL
        reasons.append(Reason("long_day", {"minutes": day.total_minutes}))
    return DayDifficulty(
        level=_clamp(level),
        walk_level=walk_level,
        drive_level=drive_level,
        effort_km=effort,
        reasons=tuple(reasons),
        confident=confident,
    )


def route_difficulty(days: Sequence[DayInput], *, synthetic: bool = False) -> RouteDifficulty:
    """Difficulty of a route from its days; one empty day when none given."""
    scored = tuple(day_difficulty(day) for day in days) or (day_difficulty(DayInput(())),)
    hardest = max(scored, key=lambda day: day.level)
    level = hardest.level
    reasons = list(hardest.reasons)
    if len(scored) >= MULTI_DAY_COUNT and hardest.level >= DEMANDING_LEVEL:
        level += 1
        reasons.append(Reason("multi_day", {"days": len(scored)}))
    confident = not synthetic and all(day.confident for day in scored)
    if not confident:
        reasons.append(Reason("low_data"))
    walk_levels = [day.walk_level for day in scored if day.walk_level is not None]
    drive_levels = [day.drive_level for day in scored if day.drive_level is not None]
    return RouteDifficulty(
        level=_clamp(level),
        walk_level=max(walk_levels) if walk_levels else None,
        drive_level=max(drive_levels) if drive_levels else None,
        days=scored,
        confidence="high" if confident else "low",
        reasons=tuple(reasons),
    )


def lowest_manual_level(auto_level: int) -> int:
    """Authors may rate a route one step below the estimate, not lower (D9)."""
    return _clamp(auto_level - 1)


def shown_level(auto_level: int, manual_level: int | None, *, editorial: bool = False) -> int:
    """What people see: the manual rating when there is one, kept within the
    author's allowance; the editors' rating as it is (D9, D13)."""
    if manual_level is None:
        return auto_level
    if editorial:
        return _clamp(manual_level)
    return _clamp(max(manual_level, lowest_manual_level(auto_level)))


# The single word older clients read and filter by (D15, D22).
LEGACY_NAMES: dict[int, str] = {1: "easy", 2: "easy", 3: "moderate", 4: "hard", 5: "extreme"}
LEGACY_LEVELS: dict[str, int] = {
    "easy": 2,
    "легкий": 2,
    "лёгкий": 2,
    "moderate": 3,
    "средний": 3,
    "hard": 4,
    "difficult": 4,
    "сложный": 4,
    "extreme": 5,
    "expert": 5,
}


def legacy_name(level: int) -> str:
    return LEGACY_NAMES[_clamp(level)]


def level_from_legacy(value: str | None) -> int | None:
    if not value:
        return None
    return LEGACY_LEVELS.get(value.casefold().strip())


# Reward multiplier by the estimate at the start of a run (D10).
REWARD_MULTIPLIERS: dict[int, float] = {1: 1.0, 2: 1.1, 3: 1.25, 4: 1.4, 5: 1.5}


def reward_multiplier(level: int | None) -> float:
    if level is None:
        return 1.0
    return REWARD_MULTIPLIERS[_clamp(level)]
