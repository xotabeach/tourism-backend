"""Spec 15 (D2, D4, D16): when a run counts and which skips are a signal."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tourism_backend.modules.admin.presentation.skip_signals_admin import (
    MIN_PEOPLE,
    summarize,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    parse_settings,
    validate_setting,
)
from tourism_backend.modules.route_execution.application.rewards import (
    completed_share_percent,
    run_counts,
)


@pytest.mark.parametrize(
    ("marked", "total", "share"),
    [
        (7, 7, 100),
        (5, 7, 71),
        (4, 7, 57),
        (3, 4, 75),
        (2, 4, 50),
        (0, 5, 0),
        (0, 0, 100),
        (9, 4, 100),
    ],
)
def test_share_is_marked_required_stops_rounded_down(marked: int, total: int, share: int) -> None:
    assert completed_share_percent(marked, total) == share


def test_a_run_counts_from_the_threshold_up() -> None:
    assert run_counts(70, 70)
    assert run_counts(71, 70)
    assert not run_counts(69, 70)
    # Five of seven is 71%: one closed stop of seven does not cost the route.
    assert run_counts(completed_share_percent(5, 7), 70)
    assert not run_counts(completed_share_percent(4, 7), 70)


def test_threshold_setting_has_bounds_and_a_default() -> None:
    assert parse_settings({}).counted_stops_percent == 70
    assert parse_settings({"af_counted_stops_percent": "80"}).counted_stops_percent == 80
    # A value that would count a run with almost nothing walked is refused,
    # and a bad stored value falls back to the default.
    with pytest.raises(ValueError, match="Допустимо"):
        validate_setting("af_counted_stops_percent", "10")
    with pytest.raises(ValueError, match="Допустимо"):
        validate_setting("af_counted_stops_percent", "101")
    assert parse_settings({"af_counted_stops_percent": "abc"}).counted_stops_percent == 70


def test_a_place_is_listed_when_enough_different_people_gave_the_reason() -> None:
    now = datetime.now(UTC)
    museum, cave, spring = uuid4(), uuid4(), uuid4()
    regular = uuid4()
    rows = [
        *[(museum, "Музей", "closed", uuid4(), now - timedelta(days=i)) for i in range(MIN_PEOPLE)],
        # The same person three times is one voice.
        *[(cave, "Пещера", "closed", regular, now - timedelta(days=i)) for i in range(3)],
        # «Не успеваю» says nothing about the place.
        *[(spring, "Родник", "no_time", uuid4(), now) for _ in range(5)],
        *[(spring, "Родник", "hard", uuid4(), now) for _ in range(MIN_PEOPLE)],
    ]

    result = summarize(rows)

    assert {(row["name"], row["label"], row["people"]) for row in result["places"]} == {
        ("Родник", "Трудно или опасно", MIN_PEOPLE),
        ("Музей", "Закрыто", MIN_PEOPLE),
    }
    assert result["below"] == 1
    assert summarize([])["places"] == []


def test_idle_close_setting_has_bounds_and_a_default() -> None:
    assert parse_settings({}).idle_close_days == 7
    assert parse_settings({"af_idle_close_days": "14"}).idle_close_days == 14
    with pytest.raises(ValueError, match="Допустимо"):
        validate_setting("af_idle_close_days", "0")
