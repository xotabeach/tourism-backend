"""Timetable periods as editors enter them (spec 12b, section 2).

A period runs on some weekdays, optionally between dates, either every N
minutes from the first departure to the last or at listed times for rare
lines. Parsing is pure so the admin form and the AI draft share it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ALL_DAYS = (1 << len(WEEKDAYS)) - 1
WORKDAYS = 0b0011111
WEEKEND = 0b1100000

# A check older than this is flagged in the line list (D8).
STALE_AFTER = timedelta(days=90)

# Speed used for stop-to-stop times when the line has none of its own.
DEFAULT_SPEED_KMH = {
    "bus": 25,
    "trolleybus": 20,
    "tram": 18,
    "share_taxi": 30,
    "train": 45,
    "ferry": 20,
    "cable_car": 10,
}

_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


class ScheduleError(ValueError):
    """What is wrong with the form, in words for the editor."""


@dataclass(frozen=True, slots=True)
class ScheduleInput:
    title: str
    days: int
    date_from: date | None
    date_to: date | None
    first_departure: time
    last_departure: time
    headway_minutes: int | None
    departures: list[time] | None
    source: str
    checked_at: date
    note: str | None


def days_mask(selected: Iterable[int]) -> int:
    """Weekday indexes (0 = Monday) into the stored bitmask."""
    mask = 0
    for index in selected:
        if 0 <= index < len(WEEKDAYS):
            mask |= 1 << index
    return mask


def days_label(mask: int) -> str:
    if mask == ALL_DAYS:
        return "ежедневно"
    if mask == WORKDAYS:
        return "будни"
    if mask == WEEKEND:
        return "выходные"
    return ", ".join(name for index, name in enumerate(WEEKDAYS) if mask & (1 << index))


def parse_times(raw: str) -> list[time]:
    """«06:10 07.40, 9:05» → sorted distinct times."""
    found = {time(int(hours), int(minutes)) for hours, minutes in _TIME.findall(raw)}
    return sorted(found)


def format_times(values: Iterable[time] | None) -> str:
    return " ".join(value.strftime("%H:%M") for value in values or [])


def _time(raw: str, field: str) -> time:
    values = parse_times(raw)
    if len(values) != 1:
        raise ScheduleError(f"{field}: время в виде ЧЧ:ММ.")
    return values[0]


def _date(raw: str, field: str, *, required: bool = False) -> date | None:
    raw = raw.strip()
    if not raw:
        if required:
            raise ScheduleError(f"{field}: обязательное поле.")
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError(f"{field}: дата в виде ГГГГ-ММ-ДД.") from exc


def parse_schedule_form(form: Mapping[str, str], *, today: date) -> ScheduleInput:
    """Validate the admin form; raises ScheduleError with the first problem."""
    title = form.get("title", "").strip()
    if not title:
        raise ScheduleError("Название периода: например «Будни, лето».")
    days = days_mask(index for index in range(len(WEEKDAYS)) if form.get(f"day_{index}") == "on")
    if not days:
        raise ScheduleError("Отметьте хотя бы один день недели.")
    date_from = _date(form.get("date_from", ""), "Действует с")
    date_to = _date(form.get("date_to", ""), "Действует по")
    if date_from and date_to and date_to < date_from:
        raise ScheduleError("Период: дата окончания раньше начала.")

    departures = parse_times(form.get("departures", ""))
    raw_headway = form.get("headway_minutes", "").strip()
    headway: int | None = None
    if raw_headway:
        try:
            headway = int(raw_headway)
        except ValueError as exc:
            raise ScheduleError("Интервал: целое число минут.") from exc
        if not 1 <= headway <= 720:
            raise ScheduleError("Интервал: от 1 до 720 минут.")
    if headway and departures:
        raise ScheduleError("Либо интервал, либо список отправлений, не оба сразу.")
    if headway:
        first = _time(form.get("first_departure", ""), "Первый рейс")
        last = _time(form.get("last_departure", ""), "Последний рейс")
        if last < first:
            raise ScheduleError("Последний рейс раньше первого.")
    elif departures:
        first, last = departures[0], departures[-1]
    else:
        raise ScheduleError("Укажите интервал с первым и последним рейсом или список отправлений.")

    source = form.get("source", "").strip()
    if not source:
        raise ScheduleError("Источник: ссылка или «звонок перевозчику, дата».")
    checked_at = _date(form.get("checked_at", ""), "Дата проверки", required=True)
    assert checked_at is not None
    if checked_at > today:
        raise ScheduleError("Дата проверки в будущем.")
    note = form.get("note", "").strip() or None
    return ScheduleInput(
        title=title[:128],
        days=days,
        date_from=date_from,
        date_to=date_to,
        first_departure=first,
        last_departure=last,
        headway_minutes=headway,
        departures=departures or None,
        source=source,
        checked_at=checked_at,
        note=note,
    )


def is_stale(checked_at: date | None, *, today: date) -> bool:
    return checked_at is None or today - checked_at > STALE_AFTER
