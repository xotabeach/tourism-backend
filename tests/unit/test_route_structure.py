"""Days and segments of a route (spec 14, step 0)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tourism_backend.modules.route_builder.application.routing import normalize_transport_mode
from tourism_backend.modules.route_execution.application.rewards import (
    MAX_DRIVE_POINTS_PER_DAY,
    MAX_POINTS,
    RouteEffort,
    SegmentEffort,
    travel_points_for_effort,
)
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    routing_snapshot_fingerprint,
)
from tourism_backend.modules.routes.application.structure_rules import (
    base_mode_for,
    implies_public_transport,
    plan_segments,
    plan_single_day,
    segment_mode_for,
)
from tourism_backend.modules.routes.infrastructure.models import Route


@pytest.mark.parametrize(
    ("spelling", "base"),
    [
        ("walking", "walk"),
        ("walk", "walk"),
        (" Pedestrian ", "walk"),
        ("bicycle", "walk"),
        (None, "walk"),
        ("something-else", "walk"),
        ("car", "car"),
        ("driving", "car"),
        ("mixed", "mixed"),
        ("public", "mixed"),
        ("public_transport", "mixed"),
    ],
)
def test_every_stored_spelling_has_a_base_mode(spelling: str | None, base: str) -> None:
    assert base_mode_for(spelling) == base


def test_public_spellings_mark_the_route_as_needing_transit() -> None:
    assert implies_public_transport("public_transport")
    assert implies_public_transport("public")
    assert not implies_public_transport("mixed")
    assert not implies_public_transport(None)


def test_mixed_routes_are_driven_until_mixing_lands() -> None:
    assert segment_mode_for("walking") == "walk"
    assert segment_mode_for("car") == "car"
    assert segment_mode_for("mixed") == "car"
    assert segment_mode_for("public_transport") == "car"


def test_driving_spelling_is_no_longer_walked() -> None:
    assert normalize_transport_mode("driving") == "car"
    assert normalize_transport_mode("walking") == "walk"
    assert normalize_transport_mode("mixed") == "mixed"


def test_router_legs_become_one_main_segment_each() -> None:
    stops = [uuid4(), uuid4(), uuid4()]
    routing = {
        "legs": [
            {"distance_meters": 1200, "duration_seconds": 900},
            {"distance_meters": 800.4, "duration_seconds": 600},
        ]
    }
    segments = plan_segments(stops, routing=routing, base_mode="walk")
    assert [(s.leg_index, s.seq, s.mode, s.role, s.origin) for s in segments] == [
        (0, 0, "walk", "main", "router"),
        (1, 0, "walk", "main", "router"),
    ]
    assert [s.distance_meters for s in segments] == [1200, 800]
    assert (segments[1].from_stop_id, segments[1].to_stop_id) == (stops[1], stops[2])


@pytest.mark.parametrize(
    "routing",
    [
        None,
        {},
        {"legs": [{"distance_meters": 10, "duration_seconds": 5}]},
        {"legs": [{"distance_meters": -1, "duration_seconds": 5}, {}]},
        {"legs": [{"distance_meters": True, "duration_seconds": 5}, {}]},
    ],
)
def test_legs_that_do_not_fit_the_stops_leave_synthetic_segments(
    routing: dict[str, object] | None,
) -> None:
    segments = plan_segments([uuid4(), uuid4(), uuid4()], routing=routing, base_mode="car")
    assert [(s.mode, s.origin, s.distance_meters) for s in segments] == [
        ("car", "synthetic", None),
        ("car", "synthetic", None),
    ]


def test_a_route_is_one_day_from_first_to_last_stop() -> None:
    stops = [uuid4(), uuid4(), uuid4()]
    [day] = plan_single_day(stops)
    assert (day.day_index, day.first_stop_id, day.last_stop_id) == (1, stops[0], stops[-1])
    assert plan_single_day([]) == []


def test_snapshot_fingerprint_sees_a_changed_segment() -> None:
    route = Route(
        id=uuid4(),
        transport_mode="walking",
        accessibility={"routing": {"provider": "valhalla"}},
        updated_at=datetime.now(UTC),
    )
    stops = [uuid4(), uuid4()]
    signature = [(stops[0], 1, uuid4()), (stops[1], 2, uuid4())]
    days = plan_single_day(stops)
    walked = plan_segments(
        stops,
        routing={"legs": [{"distance_meters": 1000, "duration_seconds": 700}]},
        base_mode="walk",
    )
    longer = plan_segments(
        stops,
        routing={"legs": [{"distance_meters": 1001, "duration_seconds": 700}]},
        base_mode="walk",
    )

    def fingerprint(segments: list) -> str:  # type: ignore[type-arg]
        return routing_snapshot_fingerprint(
            route, geometry_wkt=None, stop_signature=signature, segments=segments, days=days
        )

    assert fingerprint(walked) == fingerprint(walked)
    assert fingerprint(walked) != fingerprint(longer)


def _walk(km: float, climb: int | None = None, role: str = "main") -> SegmentEffort:
    return SegmentEffort(
        mode="walk", role=role, distance_meters=int(km * 1000), elevation_gain_meters=climb
    )


def _drive(km: float) -> SegmentEffort:
    return SegmentEffort(mode="car", role="main", distance_meters=int(km * 1000))


def test_segments_pay_walking_fully_and_driving_without_the_climb() -> None:
    effort = RouteEffort(
        completed_required_stops=2,
        elevation_gain_meters=400,
        segments=(_drive(50), _walk(2)),
    )
    # 10 base + 6 stops + 50 × 0.2 + 2 × 1.0; the route's climb is not paid
    # because it cannot be told apart from the drive.
    assert travel_points_for_effort(effort) == 28


def test_all_walking_segments_still_earn_the_route_climb() -> None:
    effort = RouteEffort(
        completed_required_stops=0,
        elevation_gain_meters=200,
        segments=(_walk(3), _walk(2)),
    )
    assert travel_points_for_effort(effort) == 10 + 5 + 10


def test_the_walk_back_to_the_car_is_not_paid() -> None:
    there = RouteEffort(completed_required_stops=0, segments=(_walk(1.5, climb=0),))
    there_and_back = RouteEffort(
        completed_required_stops=0,
        segments=(_walk(1.5, climb=0), _walk(1.5, climb=0, role="return")),
    )
    assert travel_points_for_effort(there) == travel_points_for_effort(there_and_back)


def test_driving_is_capped_per_day_and_the_cap_grows_with_days() -> None:
    long_drive = (_drive(1500),)
    one_day = RouteEffort(completed_required_stops=0, segments=long_drive)
    two_days = RouteEffort(completed_required_stops=0, segments=long_drive, day_count=2)
    assert travel_points_for_effort(one_day) == 10 + MAX_DRIVE_POINTS_PER_DAY
    assert travel_points_for_effort(two_days) == 10 + 2 * MAX_DRIVE_POINTS_PER_DAY


def test_the_route_cap_is_per_day() -> None:
    huge = (_walk(400),)
    one_day = RouteEffort(completed_required_stops=0, segments=huge)
    two_days = RouteEffort(completed_required_stops=0, segments=huge, day_count=2)
    assert travel_points_for_effort(one_day) == MAX_POINTS
    # 10 + 400 fits under 2 × 300.
    assert travel_points_for_effort(two_days) == 410


def test_transit_segments_earn_nothing_before_12b() -> None:
    bus = SegmentEffort(mode="bus", role="main", distance_meters=30_000)
    assert travel_points_for_effort(RouteEffort(completed_required_stops=0, segments=(bus,))) == 10
    unrouted = SegmentEffort(mode="bus", role="main", distance_meters=None)
    effort = RouteEffort(completed_required_stops=0, distance_meters=30_000, segments=(unrouted,))
    assert travel_points_for_effort(effort) == 10


def test_synthetic_segments_fall_back_to_the_route_length() -> None:
    blank_walk = SegmentEffort(mode="walk", role="main", distance_meters=None)
    blank_car = SegmentEffort(mode="car", role="main", distance_meters=None)
    walked = RouteEffort(completed_required_stops=0, distance_meters=5_000, segments=(blank_walk,))
    driven = RouteEffort(completed_required_stops=0, distance_meters=5_000, segments=(blank_car,))
    assert travel_points_for_effort(walked) == 15
    assert travel_points_for_effort(driven) == 11


def test_snapshots_without_segments_keep_the_old_rules() -> None:
    effort = RouteEffort(
        completed_required_stops=1,
        distance_meters=10_000,
        elevation_gain_meters=100,
        transport_mode="car",
    )
    # 10 + 3 + 10 × 0.2 + 100 / 20: the car climb is still paid here.
    assert travel_points_for_effort(effort) == 20
