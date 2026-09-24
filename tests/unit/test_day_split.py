"""Splitting a route into days (spec 14a, section 1)."""

from uuid import uuid4

from tourism_backend.modules.routes.application.day_split import (
    DayStop,
    day_norm_minutes,
    daylight_minutes,
    season_day,
    split_days,
)

_YALTA = (44.495, 34.166)  # lat, lng


def _stops(*visits: int, marks: dict[int, str] | None = None) -> list[DayStop]:
    marks = marks or {}
    return [
        DayStop(
            stop_id=uuid4(), name=f"Точка {i + 1}", visit_minutes=v, time_of_day=marks.get(i, "any")
        )
        for i, v in enumerate(visits)
    ]


def test_daylight_in_yalta_is_short_in_december_and_long_in_june() -> None:
    december = daylight_minutes(*_YALTA, 355)
    june = daylight_minutes(*_YALTA, 172)
    # 22 Dec in Yalta: sunrise 7:57, sunset 16:46.
    assert 8 * 60 + 40 <= december <= 9 * 60
    assert 15 * 60 <= june <= 15 * 60 + 40


def test_polar_edges_do_not_break_the_formula() -> None:
    assert daylight_minutes(80.0, 0.0, 172) == 24 * 60
    assert daylight_minutes(80.0, 0.0, 355) == 0


def test_seasons_pick_their_shortest_day_and_empty_means_may_to_september() -> None:
    assert season_day(["лето", "осень"]) == [243, 334]
    assert season_day(["Summer"]) == [243]
    assert season_day([]) == [273]
    assert season_day(None) == [273]
    assert season_day(["круглый год"]) == [273]


def test_norm_follows_pace_mode_children_and_winter_daylight() -> None:
    kwargs = {"lat": _YALTA[0], "lng": _YALTA[1], "seasons": ["лето"]}
    assert day_norm_minutes(pace="calm", driven=False, with_children=False, **kwargs) == 300
    assert day_norm_minutes(pace=None, driven=False, with_children=False, **kwargs) == 360
    assert day_norm_minutes(pace="calm", driven=False, with_children=True, **kwargs) == 240
    assert day_norm_minutes(pace="calm", driven=True, with_children=False, **kwargs) == 480
    # December daylight (~8 h 50 min) leaves ~6 h 50 min: a drive day shrinks to it.
    winter = day_norm_minutes(
        pace=None, driven=True, with_children=False, lat=_YALTA[0], lng=_YALTA[1], seasons=["зима"]
    )
    assert 6 * 60 + 30 <= winter < 7 * 60


def test_a_short_route_is_one_day_without_an_overnight() -> None:
    stops = _stops(60, 60, 60)
    [day] = split_days(stops, [30, 30], norm_minutes=360)
    assert (day.first_stop_id, day.last_stop_id) == (stops[0].stop_id, stops[-1].stop_id)
    assert day.overnight_note is None
    assert not day.overloaded


def test_a_long_route_breaks_before_the_stop_that_does_not_fit() -> None:
    # Each next stop costs 60 min × 1.15 of walking plus a 60 min visit, and
    # the day that passes 4 h gets a 45 min meal; a day may run 30% over its
    # 6 h norm, to 7 h 48 min: three stops fit.
    stops = _stops(60, 60, 60, 60, 60)
    days = split_days(stops, [60, 60, 60, 60], norm_minutes=360)
    assert [d.last_stop_id for d in days] == [stops[2].stop_id, stops[4].stop_id]
    assert days[1].first_stop_id == stops[3].stop_id
    assert days[0].overnight_note == "Ночлег в районе: Точка 3"
    assert days[-1].overnight_note is None
    # Deterministic: the same input gives the same days.
    assert split_days(stops, [60, 60, 60, 60], norm_minutes=360) == days


def test_a_full_day_walk_is_not_cut_in_two() -> None:
    """«Ялта · закат и панорамы пешком»: 115 min of visits, 194 min on foot."""
    stops = _stops(60, 55)
    [day] = split_days(stops, [194], norm_minutes=360)
    assert not day.overloaded


def test_a_two_stop_walk_longer_than_a_day_stays_one_overloaded_day() -> None:
    """Never a first day of just the starting stop (D25); the author is asked
    to add a stop for the night instead (D4)."""
    stops = _stops(45, 45)
    [day] = split_days(stops, [1452], norm_minutes=480)
    assert day.overloaded


def test_a_leg_longer_than_a_day_is_kept_and_flags_its_day() -> None:
    stops = _stops(30, 30, 30)
    days = split_days(stops, [10, 600], norm_minutes=360)
    assert len(days) == 2
    assert not days[0].overloaded
    assert days[1].overloaded
    assert days[1].first_stop_id == stops[2].stop_id


def test_a_night_stop_ends_its_day_and_a_dawn_stop_starts_one() -> None:
    stops = _stops(30, 30, 30, 30, marks={1: "dark", 3: "dawn"})
    days = split_days(stops, [10, 10, 10], norm_minutes=360)
    assert [(d.first_stop_id, d.last_stop_id) for d in days] == [
        (stops[0].stop_id, stops[1].stop_id),
        (stops[2].stop_id, stops[2].stop_id),
        (stops[3].stop_id, stops[3].stop_id),
    ]


def test_no_stops_no_days() -> None:
    assert split_days([], [], norm_minutes=360) == []


def _route_stops(*names: str) -> list[tuple]:
    places = {name: uuid4() for name in set(names)}
    return [(uuid4(), places[name], name) for name in names]


def test_manual_days_end_after_the_places_the_author_chose() -> None:
    from tourism_backend.modules.routes.application.structure_rules import days_from_breaks

    stops = _route_stops("Ялта", "Ливадия", "Ай-Петри", "Алупка")
    days = days_from_breaks(stops, [str(stops[1][1])])
    assert [(d.first_stop_id, d.last_stop_id) for d in days] == [
        (stops[0][0], stops[1][0]),
        (stops[2][0], stops[3][0]),
    ]
    assert {d.boundary_source for d in days} == {"manual"}
    assert days[0].overnight_note == "Ночлег в районе: Ливадия"
    assert days[1].overnight_note is None


def test_a_break_on_a_removed_place_or_the_last_stop_is_dropped() -> None:
    from tourism_backend.modules.routes.application.structure_rules import days_from_breaks

    stops = _route_stops("Ялта", "Ливадия", "Алупка")
    gone = str(uuid4())
    assert len(days_from_breaks(stops, [gone])) == 1
    assert len(days_from_breaks(stops, [str(stops[-1][1])])) == 1
    assert days_from_breaks([], [gone]) == []


def test_a_place_visited_twice_ends_the_day_at_the_visit_in_order() -> None:
    from tourism_backend.modules.routes.application.structure_rules import days_from_breaks

    stops = _route_stops("Ялта", "Ливадия", "Ялта", "Алупка")
    yalta = str(stops[0][1])
    days = days_from_breaks(stops, [yalta, yalta])
    assert [d.last_stop_id for d in days] == [stops[0][0], stops[2][0], stops[3][0]]


def test_the_daily_cap_counts_every_calendar_day_of_a_multi_day_run() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tourism_backend.modules.route_execution.application.antifraud_service import (
        _calendar_days,
    )

    started = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)  # 10:00 Moscow
    end = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)
    one_day = SimpleNamespace(started_at=started, night_pauses=0)
    three_days = SimpleNamespace(started_at=started, night_pauses=2)
    rested_once = SimpleNamespace(started_at=started, night_pauses=1)
    assert _calendar_days(one_day, end) == 1  # type: ignore[arg-type]
    assert _calendar_days(three_days, end) == 3  # type: ignore[arg-type]
    # Never more days than the walker actually ended.
    assert _calendar_days(rested_once, end) == 2  # type: ignore[arg-type]
