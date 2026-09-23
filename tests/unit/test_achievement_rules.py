"""Boundary regressions for the achievement catalogue (spec 11)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from tourism_backend.modules.achievements.facts import Facts, RunFact, StopFact, progress
from tourism_backend.modules.achievements.rules import RULES
from tourism_backend.modules.achievements.solar import sunrise_sunset
from tourism_backend.modules.routes.application.service import _difficulty_name

NOW = datetime(2026, 9, 22, 15, tzinfo=UTC)
RUN = RunFact(uuid4(), NOW, 1000, False, False, False)
STOP = StopFact(uuid4(), "swallow-nest", "yalta", 44.495, 34.166, 500, NOW, 1)


def test_catalog_has_exactly_32_rules_and_only_two_permanently_soon():
    assert len(RULES) == 32
    assert {r.slug for r in RULES.values() if r.soon} == {"group", "guide"}
    assert all(len(rule.how_to_earn) <= 240 for rule in RULES.values())


@pytest.mark.parametrize(("meters", "earned"), [(47999, False), (48000, True)])
def test_marathon_distance_boundary(meters, earned):
    values = progress(Facts(runs=[replace(RUN, distance_m=meters)]), NOW)
    assert (values["_marathoner_award"] >= 48) is earned


def test_marathon_window_and_expired_progress():
    first = replace(RUN, completed_at=NOW - timedelta(days=7), distance_m=24000)
    last = replace(RUN, distance_m=24000)
    assert progress(Facts(runs=[first, last]), NOW)["_marathoner_award"] == 48
    first = replace(first, completed_at=first.completed_at - timedelta(microseconds=1))
    assert progress(Facts(runs=[first, last]), NOW)["_marathoner_award"] == 24
    old = replace(RUN, completed_at=NOW - timedelta(days=8), distance_m=48000)
    values = progress(Facts(runs=[old]), NOW)
    assert values["marathoner"] == 0
    assert values["_marathoner_award"] == 48


@pytest.mark.parametrize(("slug", "meters"), [("distance", 100000), ("berlin", 2000000)])
def test_total_distance_threshold(slug, meters):
    for amount, expected in [(meters - 1, False), (meters, True)]:
        values = progress(Facts(runs=[replace(RUN, distance_m=amount)]), NOW)
        assert (values[slug] >= RULES[slug].target) is expected


def test_repeats_and_deleted_routes():
    assert progress(Facts(runs=[RUN, RUN]), NOW)["same-way"] == 1
    assert progress(Facts(runs=[replace(RUN, route_id=None)] * 2), NOW)["same-way"] == 0
    assert progress(Facts(runs=[replace(RUN, route_id=None)] * 10), NOW)["veteran"] == 10


@pytest.mark.parametrize(
    ("distance", "valid"), [(None, False), (-1, False), (500, True), (501, False)]
)
def test_place_distance_boundary(distance, valid):
    stop = replace(STOP, distance=distance)
    values = progress(Facts(stops=[stop], runs=[replace(RUN, stops=(stop,))]), NOW)
    assert bool(values["swallow"]) is valid
    assert values["local"] == int(valid)


def test_distinct_local_places_only():
    run = replace(RUN, stops=(STOP, STOP))
    assert progress(Facts(runs=[run] * 20), NOW)["local"] == 1
    stops = tuple(replace(STOP, place_id=uuid4()) for _ in range(20))
    assert progress(Facts(runs=[replace(RUN, stops=stops)]), NOW)["local"] == 20


def test_summer_winter_january_use_crimea_time():
    january = datetime(2025, 12, 31, 21, tzinfo=UTC)
    summer = datetime(2026, 5, 31, 21, tzinfo=UTC)
    values = progress(Facts(runs=[replace(RUN, completed_at=t) for t in [january, summer]]), NOW)
    assert values["season"] == 2
    assert values["winter"] == 1
    assert (
        progress(Facts(runs=[replace(RUN, completed_at=january - timedelta(seconds=1))]), NOW)[
            "winter"
        ]
        == 0
    )
    assert progress(Facts(runs=[replace(RUN, completed_at=None)]), NOW)["season"] == 0


def test_beach_extreme_and_generated_flags():
    runs = [replace(RUN, seaside=True, extreme=True, generated_for_user=True)] * 5
    values = progress(Facts(runs=runs), NOW)
    assert values["water"] == values["sea-breeze"] == 5
    assert values["navigator"] == values["legend-path"] == values["first-step"] == 1
    assert [_difficulty_name(i) for i in range(1, 6)] == [
        "easy",
        "easy",
        "moderate",
        "hard",
        "extreme",
    ]


@pytest.mark.parametrize(
    "slug",
    ["favorite", "social", "review", "photo", "people-author", "author", "photographer", "pen"],
)
def test_counter_thresholds(slug):
    rule = RULES[slug]
    for value, expected in [(rule.target - 1, False), (rule.target, True)]:
        assert (progress(Facts(counters={slug: value}), NOW)[slug] >= rule.target) is expected


def test_sunrise_window_and_missing_server_time():
    solar = sunrise_sunset(date(2026, 6, 21), STOP.lat, STOP.lng)
    assert solar is not None
    for at, expected in [
        (solar[0] - timedelta(seconds=1), 0),
        (solar[0], 1),
        (solar[0] + timedelta(hours=1), 1),
        (solar[0] + timedelta(hours=1, seconds=1), 0),
        (None, 0),
    ]:
        assert progress(Facts(stops=[replace(STOP, recorded_at=at)]), NOW)["sunrise"] == expected


def test_night_and_yalta_boundaries():
    day = date(2026, 9, 22)
    solar = sunrise_sunset(day, STOP.lat, STOP.lng)
    assert solar is not None
    for at, expected in [(solar[1], 0), (solar[1] + timedelta(seconds=1), 1)]:
        assert (
            progress(Facts(runs=[replace(RUN, completed_at=at, stops=(STOP,))]), NOW)["night"]
            == expected
        )
    for at, expected in [(NOW, 0), (NOW + timedelta(seconds=1), 1)]:
        assert (
            progress(Facts(runs=[replace(RUN, completed_at=at, stops=(STOP,))]), NOW)[
                "yalta-lights"
            ]
            == expected
        )


@pytest.mark.parametrize(
    ("lat", "lng", "day", "rise", "set_"),
    [
        (44.495, 34.166, date(2026, 6, 21), (5, 2), (20, 33)),
        (44.952, 34.102, date(2026, 12, 21), (8, 17), (17, 5)),
    ],
)
def test_noaa_reference_times_crimea(lat, lng, day, rise, set_):
    # Rounded reference civil times, with a 5-minute allowance for the
    # published NOAA fractional-year approximation rather than atmospheric data.
    solar = sunrise_sunset(day, lat, lng)
    assert solar is not None
    for instant, expected in zip(solar, (rise, set_), strict=True):
        target = datetime(day.year, day.month, day.day, *expected, tzinfo=UTC) - timedelta(hours=3)
        assert abs((instant - target).total_seconds()) < 300


def test_polar_day_has_no_false_sunset():
    assert sunrise_sunset(date(2026, 6, 21), 89, 34) is None
