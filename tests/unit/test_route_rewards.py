"""Travel points must reflect the effort, not just the stop count."""

from __future__ import annotations

from tourism_backend.modules.route_execution.application.rewards import (
    MAX_POINTS,
    RouteEffort,
    travel_points_for_effort,
)


def _flat_walk(stops: int = 10) -> RouteEffort:
    return RouteEffort(
        completed_required_stops=stops,
        distance_meters=8_000,
        elevation_gain_meters=20,
        max_road_angle_degrees=3.0,
        transport_mode="walk",
        difficulty="easy",
    )


def _mountain_walk(stops: int = 10) -> RouteEffort:
    return RouteEffort(
        completed_required_stops=stops,
        distance_meters=8_000,
        elevation_gain_meters=900,
        max_road_angle_degrees=27.0,
        transport_mode="walk",
        difficulty="hard",
    )


def test_mountain_route_beats_a_flat_one_with_the_same_stop_count() -> None:
    """The reason a flat per-route reward was rejected."""
    assert travel_points_for_effort(_mountain_walk()) > travel_points_for_effort(_flat_walk())


def test_partial_completion_earns_less_than_the_full_route() -> None:
    assert travel_points_for_effort(_mountain_walk(stops=3)) < travel_points_for_effort(
        _mountain_walk(stops=10)
    )


def test_starting_and_finishing_without_stops_only_earns_the_base() -> None:
    effort = RouteEffort(completed_required_stops=0, transport_mode="walk")
    assert travel_points_for_effort(effort) == 10


def test_walking_is_worth_more_per_kilometre_than_driving() -> None:
    walk = RouteEffort(
        completed_required_stops=2, distance_meters=20_000, transport_mode="walk"
    )
    drive = RouteEffort(
        completed_required_stops=2, distance_meters=20_000, transport_mode="car"
    )
    assert travel_points_for_effort(walk) > travel_points_for_effort(drive)


def test_reward_is_capped_and_never_negative() -> None:
    huge = RouteEffort(
        completed_required_stops=500,
        distance_meters=900_000,
        elevation_gain_meters=50_000,
        max_road_angle_degrees=45.0,
        transport_mode="walk",
        difficulty="hard",
    )
    assert travel_points_for_effort(huge) == MAX_POINTS

    nonsense = RouteEffort(
        completed_required_stops=-5,
        distance_meters=-100,
        elevation_gain_meters=-100,
        transport_mode="walk",
    )
    assert travel_points_for_effort(nonsense) >= 0


def test_unknown_difficulty_does_not_change_the_reward() -> None:
    known = RouteEffort(completed_required_stops=4, difficulty="easy")
    unknown = RouteEffort(completed_required_stops=4, difficulty="что-то новое")
    assert travel_points_for_effort(known) == travel_points_for_effort(unknown)
