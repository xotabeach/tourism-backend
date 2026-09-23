"""What a route keeps from its routing answer (spec 12a, sections 5 and 7)."""

from __future__ import annotations

from tourism_backend.modules.route_builder.application.routing import (
    STEEP_SEGMENT_DEGREES,
    RouteLegResult,
    RoutingResult,
    routing_details,
)


def _result(legs, *, synthetic=False, angle=12.0) -> RoutingResult:
    return RoutingResult(
        provider="valhalla",
        synthetic=synthetic,
        legs=tuple(legs),
        total_distance_meters=sum(leg.distance_meters for leg in legs),
        total_duration_seconds=sum(leg.duration_seconds for leg in legs),
        elevation_gain_meters=388,
        elevation_loss_meters=372,
        min_altitude_meters=3,
        max_altitude_meters=186,
        max_road_angle_degrees=angle,
    )


def _leg(i, meters, seconds):
    return RouteLegResult(i, i + 1, meters, seconds, None)


def test_one_leg_per_stop_pair_feeds_the_run_plan():
    details = routing_details(
        _result([_leg(0, 11863, 9000), _leg(1, 6289, 5600)]),
        stop_count=3,
        data_version="osm20260922",
    )
    assert details["legs"] == [
        {"distance_meters": 11863, "duration_seconds": 9000},
        {"distance_meters": 6289, "duration_seconds": 5600},
    ]
    assert details["provider_version"] == "osm20260922"
    assert details["elevation_gain_meters"] == 388
    assert details["max_altitude_meters"] == 186
    assert "steep_segment" not in details


def test_aggregated_or_synthetic_answers_keep_no_legs():
    aggregated = RoutingResult(
        provider="2gis",
        synthetic=False,
        legs=(RouteLegResult(0, 2, 18152, 14600, None),),
        total_distance_meters=18152,
        total_duration_seconds=14600,
    )
    assert "legs" not in routing_details(aggregated, stop_count=3, data_version=None)
    synthetic = _result([_leg(0, 100, 60), _leg(1, 100, 60)], synthetic=True)
    assert "legs" not in routing_details(synthetic, stop_count=3, data_version=None)


def test_steep_stretch_is_flagged_never_blocked():
    details = routing_details(
        _result([_leg(0, 900, 1200)], angle=STEEP_SEGMENT_DEGREES + 1),
        stop_count=2,
        data_version=None,
    )
    assert details["steep_segment"] is True
