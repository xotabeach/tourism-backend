"""What an author's route is checked for before it reaches the moderator.

BACKEND-41 (spec 15, D18). Until now only «есть фото» and «есть старт и
финиш» were checked, so a 169 km walk in one day went to review as easily
as a city stroll. The checks here are the numbers the routing already gave:

- a blocker stops the submission and its text goes to the author as is;
- a warning goes to the moderator with the figures, the author is not held
  up by it.

DB-free on purpose: ``route_drafts.submit_user_route`` gathers the facts,
this module only decides, and the same report is stored on the route for
the admin page.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

#: Bumped when a limit below changes, so a stored report says which rules
#: it was made by.
POLICY_VERSION = 1

Transport = Literal["walk", "car"]


@dataclass(frozen=True, slots=True)
class _Limits:
    warn_km_per_day: float
    block_km_per_day: float
    warn_hours_per_day: float
    block_hours_per_day: float
    #: Straight line between two neighbouring stops.
    warn_gap_km: float
    #: Farthest two stops of the whole route, per day.
    warn_spread_km_per_day: float


# A walking day of 25 km is a long hike already; 45 km is not a day at all.
# A driving day is bounded by Crimea itself: about 350 km across.
LIMITS: dict[Transport, _Limits] = {
    "walk": _Limits(
        warn_km_per_day=25,
        block_km_per_day=45,
        warn_hours_per_day=8,
        block_hours_per_day=14,
        warn_gap_km=12,
        warn_spread_km_per_day=25,
    ),
    "car": _Limits(
        warn_km_per_day=400,
        block_km_per_day=900,
        warn_hours_per_day=9,
        block_hours_per_day=16,
        warn_gap_km=150,
        warn_spread_km_per_day=250,
    ),
}

#: Start and finish closer than this: probably one place picked twice.
TOO_SHORT_METERS = 300

#: Roads are longer than the straight line; used only when the router gave
#: no length at all (it refused the route), so a far route is still caught.
DETOUR_FACTOR = 1.25

# Codes the routers put in ``warnings``; anything else is shown as it is.
_ROUTER_WARNINGS = {
    "synthetic_straight_line": "Линия прямая, маршрутизатор её не строил",
    "stub_routing_not_navigation_grade": "Линия прямая, маршрутизатор её не строил",
    "pedestrian_highway_filter_violated": "Пеший путь идёт по трассе",
    "ferry_schedule_and_access_unknown": "На пути паром",
    "dirt_road_surface_requires_review": "Есть грунтовые дороги",
    "stairs_require_review": "Есть лестницы",
}


@dataclass(frozen=True, slots=True)
class SubmitFacts:
    transport: Transport
    #: From the routing answer stored on the route; None when it gave none.
    distance_meters: int | None
    movement_seconds: int | None
    elevation_gain_meters: int | None
    day_count: int
    #: A road line from a router, not straight segments between the stops.
    line_drawn: bool
    #: A real router is configured. Without one (local runs, tests) every
    #: line is straight and that is no fault of the author.
    real_router: bool
    has_author_points: bool
    region_count: int
    #: (lng, lat) of the stops in order.
    stop_points: Sequence[tuple[float, float]] = ()
    difficulty_level: int | None = None
    router_warnings: Sequence[str] = ()
    road_types: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class SubmitProblem:
    code: str
    message: str


@dataclass(slots=True)
class SubmitReport:
    blockers: list[SubmitProblem] = field(default_factory=list)
    warnings: list[SubmitProblem] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.blockers

    def as_json(self, *, checked_at: datetime) -> dict[str, Any]:
        """What ``routes.accessibility["submit_check"]`` keeps."""
        return {
            "policy_version": POLICY_VERSION,
            "checked_at": checked_at.isoformat(),
            "passed": self.passed,
            "blockers": [{"code": p.code, "message": p.message} for p in self.blockers],
            "warnings": [{"code": p.code, "message": p.message} for p in self.warnings],
            "metrics": self.metrics,
        }


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lng, lat) points."""
    lng1, lat1, lng2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def _num(value: float) -> str:
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}".replace(".", ",")


def check_submission(facts: SubmitFacts) -> SubmitReport:
    report = SubmitReport()
    limits = LIMITS[facts.transport]
    days = max(1, facts.day_count)
    word = "пешком" if facts.transport == "walk" else "на машине"

    gaps = [_km(a, b) for a, b in zip(facts.stop_points, facts.stop_points[1:], strict=False)]
    estimated = facts.distance_meters is None and bool(gaps)
    km = (
        facts.distance_meters / 1000
        if facts.distance_meters is not None
        else sum(gaps) * DETOUR_FACTOR
        if estimated
        else None
    )
    hours = facts.movement_seconds / 3600 if facts.movement_seconds is not None else None
    spread = max(
        (_km(a, b) for i, a in enumerate(facts.stop_points) for b in facts.stop_points[i + 1 :]),
        default=0.0,
    )
    report.metrics = {
        "transport": facts.transport,
        "distance_km": round(km, 1) if km is not None else None,
        "distance_estimated": estimated,
        "movement_hours": round(hours, 1) if hours is not None else None,
        "elevation_gain_meters": facts.elevation_gain_meters,
        "day_count": days,
        "difficulty_level": facts.difficulty_level,
        "line_drawn": facts.line_drawn,
        "longest_gap_km": round(max(gaps, default=0.0), 1),
        "spread_km": round(spread, 1),
        "road_types": sorted(set(facts.road_types)),
    }

    if facts.region_count > 1:
        report.blockers.append(
            SubmitProblem(
                "invalid_route_region",
                "Точки маршрута из разных регионов. Оставьте точки одного региона",
            )
        )

    # The author's own points are checked even without a real router: the
    # straight line to them is all the moderator would have (D18).
    if not facts.line_drawn and (facts.has_author_points or facts.real_router):
        report.blockers.append(
            SubmitProblem(
                "route_line_required",
                "Не получилось проложить маршрут по дорогам и тропам между точками. "
                "Попробуйте отправить позже или поменяйте точки",
            )
        )
    elif not facts.line_drawn:
        report.warnings.append(
            SubmitProblem("route_line_synthetic", "Линия прямая, маршрутизатор не подключён")
        )

    if km is not None:
        per_day = km / days
        if per_day > limits.block_km_per_day:
            report.blockers.append(
                SubmitProblem(
                    "route_too_long",
                    f"Слишком длинный маршрут {word}: {_num(per_day)} км за день. "
                    f"Разбейте его на дни или уберите дальние точки",
                )
            )
        elif per_day > limits.warn_km_per_day:
            report.warnings.append(
                SubmitProblem("route_long_day", f"{_num(per_day)} км за день {word}")
            )
        if not estimated and km * 1000 < TOO_SHORT_METERS:
            report.warnings.append(
                SubmitProblem(
                    "route_very_short",
                    f"Весь маршрут {km * 1000:.0f} м: точки почти в одном месте",
                )
            )
        if estimated:
            report.warnings.append(
                SubmitProblem(
                    "route_distance_unknown",
                    "Маршрутизатор не сообщил длину, она оценена по прямой",
                )
            )

    if hours is not None:
        per_day_hours = hours / days
        if per_day_hours > limits.block_hours_per_day:
            report.blockers.append(
                SubmitProblem(
                    "route_too_long_time",
                    f"В пути {_num(per_day_hours)} ч за день {word}, столько за день не "
                    f"пройти. Разбейте маршрут на дни или уберите дальние точки",
                )
            )
        elif per_day_hours > limits.warn_hours_per_day:
            report.warnings.append(
                SubmitProblem("route_long_hours", f"{_num(per_day_hours)} ч в пути за день")
            )

    longest = max(gaps, default=0.0)
    if longest > limits.warn_gap_km:
        report.warnings.append(
            SubmitProblem(
                "route_stops_far_apart",
                f"Между соседними точками {_num(longest)} км по прямой",
            )
        )
    if spread / days > limits.warn_spread_km_per_day:
        report.warnings.append(
            SubmitProblem(
                "route_stops_spread",
                f"Точки разбросаны на {_num(spread)} км, это не один район",
            )
        )

    # A route too long for a day is what the author can fix; that its line
    # does not build usually follows from it, so that is said last.
    report.blockers.sort(key=lambda problem: problem.code == "route_line_required")

    seen: set[str] = set()
    for code in facts.router_warnings:
        text = _ROUTER_WARNINGS.get(code, code)
        if text in seen or (not facts.line_drawn and code in _STRAIGHT_LINE_CODES):
            continue
        seen.add(text)
        report.warnings.append(SubmitProblem(f"router:{code}", text))
    return report


_STRAIGHT_LINE_CODES = frozenset({"synthetic_straight_line", "stub_routing_not_navigation_grade"})
