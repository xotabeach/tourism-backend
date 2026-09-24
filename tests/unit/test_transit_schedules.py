from datetime import date, time
from typing import Any

import pytest

from tourism_backend.modules.transit.application.schedule_draft import (
    draft_schedule,
    draft_to_forms,
)
from tourism_backend.modules.transit.application.schedules import (
    ALL_DAYS,
    WORKDAYS,
    ScheduleError,
    days_label,
    is_stale,
    parse_schedule_form,
    parse_times,
)

TODAY = date(2026, 9, 24)


def _form(**extra: str) -> dict[str, str]:
    return {
        "title": "Будни",
        **{f"day_{index}": "on" for index in range(5)},
        "source": "https://example.test/51",
        "checked_at": "2026-09-20",
        **extra,
    }


def test_interval_period() -> None:
    data = parse_schedule_form(
        _form(headway_minutes="20", first_departure="6:00", last_departure="22.30"), today=TODAY
    )
    assert data.days == WORKDAYS
    assert (data.first_departure, data.last_departure) == (time(6, 0), time(22, 30))
    assert data.headway_minutes == 20
    assert data.departures is None


def test_listed_departures_set_first_and_last() -> None:
    data = parse_schedule_form(_form(departures="09:15, 06:10 07:40 06:10"), today=TODAY)
    assert data.departures == [time(6, 10), time(7, 40), time(9, 15)]
    assert (data.first_departure, data.last_departure) == (time(6, 10), time(9, 15))
    assert data.headway_minutes is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"headway_minutes": "20"}, "Первый рейс"),
        ({"departures": "06:10", "headway_minutes": "20"}, "не оба"),
        ({}, "интервал"),
        ({"departures": "06:10", "source": " "}, "Источник"),
        ({"departures": "06:10", "checked_at": "2026-10-01"}, "будущем"),
        ({"departures": "06:10", "date_from": "2026-09-01", "date_to": "2026-08-01"}, "раньше"),
        (
            {"headway_minutes": "20", "first_departure": "22:00", "last_departure": "06:00"},
            "раньше первого",
        ),
    ],
)
def test_form_errors_speak_to_the_editor(extra: dict[str, str], message: str) -> None:
    with pytest.raises(ScheduleError, match=message):
        parse_schedule_form(_form(**extra), today=TODAY)


def test_no_weekday_is_an_error() -> None:
    form = {key: value for key, value in _form(departures="06:10").items() if "day_" not in key}
    with pytest.raises(ScheduleError, match="день недели"):
        parse_schedule_form(form, today=TODAY)


def test_labels_and_staleness() -> None:
    assert days_label(ALL_DAYS) == "ежедневно"
    assert days_label(0b0000101) == "Пн, Ср"
    assert parse_times("с 7:05 до 25:00") == [time(7, 5)]
    assert not is_stale(date(2026, 7, 1), today=TODAY)
    assert is_stale(date(2026, 6, 1), today=TODAY)


def test_draft_keeps_only_what_parses() -> None:
    forms, warning = draft_to_forms(
        {
            "periods": [
                {
                    "title": "Лето",
                    "days": [0, 6, 9],
                    "departures": ["7:10", "bad", "18:45"],
                    "headway_minutes": None,
                    "date_from": "2026-06-01",
                },
                "мусор",
            ],
            "warning": "не ясно, от какой остановки",
        }
    )
    assert forms == [
        {
            "title": "Лето",
            "date_from": "2026-06-01",
            "date_to": "",
            "first_departure": "",
            "last_departure": "",
            "note": "",
            "headway_minutes": "",
            "departures": "07:10 18:45",
            "day_0": "on",
            "day_6": "on",
        }
    ]
    assert warning == "не ясно, от какой остановки"


class _Completer:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def complete_text(self, *, system: str, user: str, max_tokens: int) -> str:
        self.calls.append({"system": system, "user": user})
        return self.answer


async def test_draft_calls_the_model_once_and_rejects_non_json() -> None:
    completer = _Completer(
        '```json\n{"periods": [{"title": "Ежедневно", "days": [0,1,2,3,4,5,6], '
        '"headway_minutes": 15, "first_departure": "05:30", "last_departure": "23:00"}]}\n```'
    )
    forms, _ = await draft_schedule(
        completer, line_name="Троллейбус 51", direction=None, text="каждые 15 минут"
    )
    assert forms[0]["headway_minutes"] == "15"
    assert "Троллейбус 51" in completer.calls[0]["user"]

    with pytest.raises(ValueError, match="no JSON"):
        await draft_schedule(_Completer("не знаю"), line_name="x", direction=None, text="y")
