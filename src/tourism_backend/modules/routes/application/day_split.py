"""Split a route into days by its norms and daylight (spec 14a, section 1).

A pure, deterministic rule: the same stops, legs and parameters always give
the same days. The day's norm comes from the route's pace, children and
base mode, as in the trip plan (itinerary.py), and never exceeds the
daylight of the shortest day among the route's seasons, one hour after
sunrise to one hour before sunset, clamped to 6-14 hours (D9, R6).

A day may run up to 30% over its norm before it is cut, and the first day
is never just the starting stop: a two-stop walk too long for one day stays
one overloaded day, so the author sees «добавьте точку для ночлега» (D4,
D25). A stop marked «в тёмное время» ends its day there; one marked «к
рассвету» starts a new day (D12). Neither makes a day longer. Multi-day
stays planned by the editors are set by hand, not guessed here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

# Minutes of travel and visits a day holds, by pace (itinerary.py).
_PACE_MINUTES = {"calm": 300, "moderate": 360, "active": 480}
_DRIVE_MINUTES = 480
_CHILDREN_FACTOR = 0.8
# Transfers, a meal and rests on top of the bare legs, as the trip plan does.
_MARGIN = 1.15
_MEAL_MINUTES = 45
# A full day of 7-8 hours is normal: a day is cut only once it runs this
# much over its norm (spec 14, D25).
_TOLERANCE = 1.3
_MIN_WINDOW_MINUTES = 6 * 60
_MAX_WINDOW_MINUTES = 14 * 60

# The last day of each season is its shortest one; an empty list means the
# usual May to September travel season (R6), whose shortest day is 30 Sep.
_SEASON_LAST_DAY: dict[str, int] = {
    "весна": 151,
    "spring": 151,
    "лето": 243,
    "summer": 243,
    "осень": 334,
    "autumn": 334,
    "fall": 334,
    "зима": 355,
    "winter": 355,
}
_DEFAULT_SEASON_DAY = 273


@dataclass(frozen=True, slots=True)
class DayStop:
    stop_id: UUID
    name: str
    visit_minutes: int
    time_of_day: str = "any"


@dataclass(frozen=True, slots=True)
class SplitDay:
    day_index: int
    first_stop_id: UUID
    last_stop_id: UUID
    overloaded: bool = False
    overnight_note: str | None = None


def daylight_minutes(lat: float, lng: float, day_of_year: int) -> int:
    """Minutes between sunrise and sunset (NOAA sunrise equation)."""
    decl = math.radians(23.44) * math.sin(math.radians(360 / 365 * (day_of_year - 81)))
    phi = math.radians(lat)
    # Sun centre 0.833° below the horizon: refraction and the solar disc.
    cos_h = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (
        math.cos(phi) * math.cos(decl)
    )
    if cos_h <= -1:
        return 24 * 60
    if cos_h >= 1:
        return 0
    return round(2 * math.degrees(math.acos(cos_h)) / 15 * 60)


def season_day(seasons: Sequence[str] | None) -> list[int]:
    """Days of the year to size a route's day by: each season's shortest."""
    days = [
        _SEASON_LAST_DAY[s.casefold().strip()]
        for s in seasons or ()
        if s.casefold().strip() in _SEASON_LAST_DAY
    ]
    return sorted(set(days)) or [_DEFAULT_SEASON_DAY]


def day_norm_minutes(
    *,
    pace: str | None,
    driven: bool,
    with_children: bool,
    lat: float | None,
    lng: float | None,
    seasons: Sequence[str] | None,
) -> int:
    """How many minutes of legs and visits one day of this route holds."""
    norm = _DRIVE_MINUTES if driven else _PACE_MINUTES.get(pace or "", 360)
    if with_children:
        norm = int(norm * _CHILDREN_FACTOR)
    if lat is None or lng is None:
        return norm
    daylight = min(daylight_minutes(lat, lng, day) for day in season_day(seasons))
    window = min(_MAX_WINDOW_MINUTES, max(_MIN_WINDOW_MINUTES, daylight - 120))
    return min(norm, window)


def split_days(
    stops: Sequence[DayStop],
    leg_minutes: Sequence[int],
    *,
    norm_minutes: int,
) -> list[SplitDay]:
    """Days over ``stops``; ``leg_minutes[i]`` leads from stop i to stop i+1."""
    if not stops:
        return []
    days: list[SplitDay] = []
    first = 0
    used = stops[0].visit_minutes
    overloaded = False

    def close(last: int) -> None:
        nonlocal first
        days.append(
            SplitDay(
                day_index=len(days) + 1,
                first_stop_id=stops[first].stop_id,
                last_stop_id=stops[last].stop_id,
                overloaded=overloaded,
                overnight_note=f"Ночлег в районе: {stops[last].name}",
            )
        )
        first = last + 1

    for index in range(1, len(stops)):
        stop = stops[index]
        leg = math.ceil(max(0, leg_minutes[index - 1]) * _MARGIN)
        step = leg + stop.visit_minutes
        dawn_start = stop.time_of_day == "dawn"
        after_dark = stops[index - 1].time_of_day == "dark"
        meal = _MEAL_MINUTES if used + step > 4 * 60 and used <= 4 * 60 else 0
        over = used + step + meal > norm_minutes * _TOLERANCE
        lone_start = not days and first == index - 1
        if dawn_start or after_dark or (over and not lone_start):
            close(index - 1)
            used, overloaded = 0, False
            meal = 0
        used += step + meal
        if leg > norm_minutes:
            overloaded = True
    close(len(stops) - 1)
    last = days[-1]
    days[-1] = SplitDay(
        day_index=last.day_index,
        first_stop_id=last.first_stop_id,
        last_stop_id=last.last_stop_id,
        overloaded=last.overloaded,
        overnight_note=None,
    )
    return days
