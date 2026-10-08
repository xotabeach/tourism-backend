"""Spec 15 (D1, D14, D15): only the legs that were walked are paid."""

import pytest

from tourism_backend.modules.route_execution.application.rewards import (
    STEEP_SLOPE_BONUS,
    RouteEffort,
    SegmentEffort,
    StopState,
    paid_leg_positions,
    paid_way_share,
    travel_points_for_effort,
)


def _stop(position: int, *, marked: bool = False, skip: str | None = None, optional: bool = False):
    return StopState(position=position, is_optional=optional, marked=marked, skip_reason=skip)


def test_a_marked_stop_and_a_closed_one_pay_their_leg() -> None:
    stops = [
        _stop(1, marked=True),
        _stop(2, skip="closed"),
        _stop(3, skip="no_time"),
        _stop(4, skip="hard"),
        _stop(5, skip="other"),
        _stop(6, marked=True),
    ]
    assert paid_leg_positions(stops, run_completed=True) == {1, 2, 6}


def test_an_untouched_optional_stop_does_not_break_a_completed_run() -> None:
    stops = [_stop(1, marked=True), _stop(2, optional=True), _stop(3, marked=True)]
    assert paid_leg_positions(stops, run_completed=True) == {1, 2, 3}
    skipped = [
        _stop(1, marked=True),
        _stop(2, optional=True, skip="no_time"),
        _stop(3, marked=True),
    ]
    assert paid_leg_positions(skipped, run_completed=True) == {1, 3}


def test_an_early_end_pays_only_up_to_the_last_reached_stop() -> None:
    stops = [
        _stop(1, marked=True),
        _stop(2, optional=True),
        _stop(3, marked=True),
        _stop(4, optional=True),
        _stop(5),
    ]
    assert paid_leg_positions(stops, run_completed=False) == {1, 2, 3}
    assert paid_leg_positions([_stop(1), _stop(2)], run_completed=False) == set()


def test_paid_share_goes_by_segment_distance_and_ignores_the_way_back() -> None:
    walk = SegmentEffort(mode="walk", role="main", distance_meters=3000)
    drive = SegmentEffort(mode="car", role="main", distance_meters=1000)
    back = SegmentEffort(mode="walk", role="return", distance_meters=9000)
    bus = SegmentEffort(mode="bus", role="main", distance_meters=9000)
    share = paid_way_share(
        [(walk, True), (drive, False), (back, False), (bus, False)],
        [(100, True), (100, False)],
    )
    assert share == pytest.approx(0.75)


def test_paid_share_falls_back_to_leg_lengths_then_to_their_count() -> None:
    unknown = SegmentEffort(mode="car", role="main", distance_meters=None)
    segments = [(unknown, True), (unknown, False)]
    # A short skipped leg costs little of a long route.
    assert paid_way_share(segments, [(319, False), (22554, True), (472, True)]) == pytest.approx(
        (22554 + 472) / (319 + 22554 + 472)
    )
    assert paid_way_share(segments, [(None, True), (500, False), (500, False)]) == pytest.approx(
        1 / 3
    )
    assert paid_way_share([], []) == 1.0
    assert paid_way_share([], [(100, True), (100, True)]) == 1.0


def _effort(**overrides: object) -> RouteEffort:
    base: dict[str, object] = {
        "completed_required_stops": 4,
        "max_road_angle_degrees": 25.0,
        "difficulty_level": 5,
        "segments": (
            SegmentEffort(
                mode="walk", role="main", distance_meters=4000, elevation_gain_meters=200
            ),
            SegmentEffort(
                mode="walk", role="main", distance_meters=4000, elevation_gain_meters=200
            ),
        ),
    }
    return RouteEffort(**{**base, **overrides})  # type: ignore[arg-type]


def test_a_whole_route_is_paid_exactly_as_before() -> None:
    assert travel_points_for_effort(_effort()) == travel_points_for_effort(_effort(paid_share=1.0))


def test_a_skipped_part_loses_the_slope_bonus_and_part_of_the_multiplier() -> None:
    whole = travel_points_for_effort(_effort())
    half_segments = _effort().segments[:1]
    same_way_full_premium = travel_points_for_effort(
        _effort(completed_required_stops=3, segments=half_segments)
    )
    half = travel_points_for_effort(
        _effort(completed_required_stops=3, segments=half_segments, paid_share=0.5)
    )
    assert half < same_way_full_premium < whole
    # With nothing of the way paid the multiplier is gone entirely.
    none = travel_points_for_effort(
        _effort(completed_required_stops=1, segments=(), paid_share=0.0, difficulty_level=5)
    )
    plain = travel_points_for_effort(
        _effort(
            completed_required_stops=1,
            segments=(),
            paid_share=0.0,
            difficulty_level=1,
            max_road_angle_degrees=0.0,
        )
    )
    assert none == plain
    assert STEEP_SLOPE_BONUS > 0
