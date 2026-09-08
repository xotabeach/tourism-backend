"""Deterministic, provisional day plans from routed legs and visit durations.

An overnight requirement is not a hotel reservation. Long indivisible legs
remain visibly flagged: splitting a trail at an invented lodging point would
hide the very gap the traveller needs to resolve.
"""

from datetime import date
from math import ceil
from typing import Literal

from pydantic import BaseModel, Field

from tourism_backend.modules.route_builder.application.routing import RoutingResult


class TripEventOut(BaseModel):
    kind: Literal["travel", "visit", "meal_break", "rest", "overnight_needed"]
    title: str
    start_minute: int = Field(ge=0)
    duration_minutes: int = Field(ge=0)
    place_id: str | None = None


class TripDayOut(BaseModel):
    day: int = Field(ge=1)
    events: list[TripEventOut]


class TripPlanOut(BaseModel):
    start_date: date | None = None
    timezone: str = "Europe/Moscow"
    provisional: bool = True
    days: list[TripDayOut]
    warnings: list[str] = Field(default_factory=list)


def build_trip_plan(
    *,
    stops: list[tuple[str, str, int]],
    routing: RoutingResult,
    pace: str,
    transport_mode: str | None,
    with_children: bool | None,
    start_date: date | None = None,
) -> TripPlanOut:
    walking = transport_mode in {None, "walk"}
    daily_minutes = {"calm": 300, "moderate": 360, "active": 480}.get(pace, 360)
    if not walking:
        daily_minutes = 480
    if with_children:
        daily_minutes = int(daily_minutes * 0.8)
    start = 9 * 60
    clock = start
    continuous_travel = 0
    meal_taken = False
    days: list[TripDayOut] = []
    events: list[TripEventOut] = []
    warnings = ["Время ориентировочное: часы работы мест и наличие ночлега нужно уточнить."]
    if routing.synthetic:
        warnings.append("Переходы оценены приблизительно; дорожный путь пока не подтверждён.")
    legs = {leg.to_index: leg for leg in routing.legs}

    def add(
        kind: Literal["travel", "visit", "meal_break", "rest", "overnight_needed"],
        title: str,
        minutes: int,
        place_id: str | None = None,
    ) -> None:
        nonlocal clock
        events.append(
            TripEventOut(
                kind=kind,
                title=title,
                start_minute=clock,
                duration_minutes=minutes,
                place_id=place_id,
            )
        )
        clock += minutes

    for index, (place_id, name, visit_minutes) in enumerate(stops):
        leg = legs.get(index)
        travel = ceil(leg.duration_seconds / 60) if leg else 0
        # Allow a modest transfer margin; do not claim arrival to the minute.
        travel = ceil(travel * 1.15 / 5) * 5
        visit = max(5, visit_minutes)
        rest = 15 if continuous_travel + travel >= (90 if walking else 180) else 0
        meal = 45 if not meal_taken and clock + travel + visit >= 13 * 60 else 0
        if events and clock + travel + visit + rest + meal > start + daily_minutes:
            previous_id, previous_name, _ = stops[index - 1]
            add("overnight_needed", f"Выбрать ночлег рядом: {previous_name}", 0, previous_id)
            days.append(TripDayOut(day=len(days) + 1, events=events))
            events = []
            clock = start
            continuous_travel = 0
            meal_taken = False
            rest = 15 if travel >= (90 if walking else 180) else 0
            meal = 45 if clock + travel + visit >= 13 * 60 else 0
        if travel:
            add("travel", f"{'Переход' if walking else 'Переезд'} к месту «{name}»", travel)
            continuous_travel += travel
        if rest:
            add("rest", "Отдых после дороги", rest, place_id)
            continuous_travel = 0
        if meal:
            add("meal_break", "Перерыв на еду — место можно выбрать позже", meal, place_id)
            meal_taken = True
        add("visit", name, visit, place_id)
        if visit >= 30:
            continuous_travel = 0
        if clock > start + daily_minutes:
            warnings.append(
                f"Участок к месту «{name}» длиннее комфортного дня. "
                "Нужен другой транспорт или проверенная промежуточная остановка."
            )
    if events:
        days.append(TripDayOut(day=len(days) + 1, events=events))
    return TripPlanOut(start_date=start_date, days=days, warnings=warnings)
