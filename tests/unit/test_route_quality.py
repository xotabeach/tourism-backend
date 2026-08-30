"""Pure tests for the route quality policy."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tourism_backend.modules.route_builder.application.place_picker import PickedPlace
from tourism_backend.modules.route_builder.application.route_quality import (
    RoadEventSignal,
    TerrainFeatureSignal,
    active_road_event_blockers,
    assess_route_quality,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteLegResult,
    RoutingResult,
)


def _routing(**changes: object) -> RoutingResult:
    values: dict[str, object] = {
        "provider": "2gis",
        "synthetic": False,
        "legs": (
            RouteLegResult(
                from_index=0,
                to_index=1,
                distance_meters=1_200,
                duration_seconds=900,
                geometry_wkt="LINESTRING(34.1 44.5, 34.11 44.51, 34.12 44.52, 34.13 44.53)",
            ),
        ),
        "total_distance_meters": 1_200,
        "total_duration_seconds": 900,
        "geometry_wkt": "LINESTRING(34.1 44.5, 34.11 44.51, 34.12 44.52, 34.13 44.53)",
        "elevation_gain_meters": 80,
        "max_road_angle_degrees": 8,
    }
    values.update(changes)
    return RoutingResult(**values)  # type: ignore[arg-type]


def test_synthetic_route_remains_usable_but_explicitly_unverified() -> None:
    assessment = assess_route_quality(
        _routing(provider="stub", synthetic=True, geometry_wkt=None),
        transport_mode="walk",
        pace="calm",
    )

    assert assessment.status == "unverified"
    assert assessment.usable_for_private_draft is True
    assert "not_navigation_grade" in assessment.warnings


def test_provider_route_without_geometry_is_unusable() -> None:
    assessment = assess_route_quality(
        _routing(geometry_wkt=None),
        transport_mode="walk",
        pace="moderate",
    )

    assert assessment.status == "unusable"
    assert assessment.usable_for_private_draft is False
    assert "provider_geometry_missing" in assessment.warnings


def test_pedestrian_highway_filter_violation_needs_review() -> None:
    assessment = assess_route_quality(
        _routing(road_types=("highway",)),
        transport_mode="walk",
        pace="active",
    )

    assert assessment.status == "needs_review"
    assert assessment.usable_for_private_draft is True
    assert "pedestrian_highway_filter_violated" in assessment.warnings


def test_route_above_requested_pace_needs_review() -> None:
    assessment = assess_route_quality(
        _routing(elevation_gain_meters=550, max_road_angle_degrees=18),
        transport_mode="walk",
        pace="calm",
    )

    assert assessment.status == "needs_review"
    assert "slope_above_requested_pace" in assessment.warnings
    assert "elevation_gain_above_requested_pace" in assessment.warnings


def test_sound_provider_route_is_never_overstated_as_fully_verified() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
    )

    assert assessment.status == "verified_with_warnings"
    assert "terrain_access_not_independently_verified" in assessment.warnings


def _stop(**changes: object) -> PickedPlace:
    values: dict[str, object] = {
        "place_id": uuid4(),
        "name": "Точка",
        "short_description": None,
        "recommended_visit_minutes": 30,
    }
    values.update(changes)
    return PickedPlace(**values)  # type: ignore[arg-type]


def test_independent_gate_blocks_closed_stop_and_explicit_child_mismatch() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
        stops=[
            _stop(temporary_closure_status="closed"),
            _stop(suitable_for_children=False),
        ],
        with_children=True,
    )

    assert assessment.status == "unusable"
    assert "stop_temporarily_closed" in assessment.warnings
    assert "stop_not_suitable_for_children" in assessment.warnings


def test_independent_gate_marks_water_surface_and_season_as_review() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="moderate",
        stops=[
            _stop(
                temporary_closure_status="partial",
                surface="gravel",
                seasonality=("summer",),
                accessibility={"water_crossing": True},
                safety_warnings=("steep cliff",),
            )
        ],
        season="winter",
    )

    assert assessment.status == "needs_review"
    assert "water_crossing_requires_independent_review" in assessment.warnings
    assert "stop_surface_requires_review" in assessment.warnings
    assert "stop_seasonality_mismatch" in assessment.warnings
    assert "stop_safety_warning_requires_review" in assessment.warnings


def test_synthetic_route_with_required_boat_access_is_not_usable() -> None:
    assessment = assess_route_quality(
        _routing(provider="stub", synthetic=True, geometry_wkt=None),
        transport_mode="walk",
        stops=[_stop(accessibility={"requires_boat": True})],
    )

    assert assessment.status == "unusable"
    assert "stop_requires_unsafe_access" in assessment.warnings


def test_active_closure_for_requested_mode_is_unusable() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="car",
        road_events=(
            RoadEventSignal(
                status="active",
                event_kind="closure",
                affects_transport=("driving",),
            ),
        ),
    )

    assert assessment.status == "unusable"
    assert "road_event_active_closure" in assessment.warnings


def test_event_for_another_mode_is_ignored_and_expired_event_is_ignored() -> None:
    now = datetime.now(UTC)
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        as_of=now,
        road_events=(
            RoadEventSignal(
                status="active",
                event_kind="closure",
                affects_transport=("driving",),
            ),
            RoadEventSignal(
                status="active",
                event_kind="closure",
                ends_at=now - timedelta(minutes=1),
            ),
        ),
    )

    assert assessment.status == "verified_with_warnings"
    assert "road_event_active_closure" not in assessment.warnings


def test_scheduled_restriction_needs_review_without_hard_block() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        road_events=(
            RoadEventSignal(
                status="scheduled",
                event_kind="restriction",
                affects_transport=("walking",),
            ),
        ),
    )

    assert assessment.status == "needs_review"
    assert "road_event_scheduled_restriction" in assessment.warnings


def test_execution_recheck_uses_same_mode_and_ignores_resolved_events() -> None:
    blockers = active_road_event_blockers(
        (
            RoadEventSignal(
                status="active",
                event_kind="closure",
                affects_transport=("car",),
            ),
            RoadEventSignal(
                status="resolved",
                event_kind="closure",
                affects_transport=("all",),
            ),
        ),
        transport_mode="walk",
    )
    assert blockers == ()


def test_two_point_long_geometry_needs_review_as_straight_line() -> None:
    assessment = assess_route_quality(
        _routing(geometry_wkt="LINESTRING(34.1 44.5, 34.13 44.53)"),
        transport_mode="walk",
        pace="calm",
    )

    assert assessment.status == "needs_review"
    assert "geometry_looks_like_straight_line" in assessment.warnings
    assert assessment.policy_version == "v2"


def test_osm_private_access_without_foot_permission_is_unusable() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
        stops=[_stop(osm_tags={"access": "private", "highway": "path"})],
    )

    assert assessment.status == "unusable"
    assert "osm_access_forbidden" in assessment.warnings
    assert "terrain_access_not_independently_verified" not in assessment.warnings


def test_osm_ford_and_waterway_need_review_without_blanket_unverified() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="moderate",
        stops=[
            _stop(
                osm_tags={
                    "ford": "yes",
                    "waterway": "stream",
                    "access": "yes",
                    "foot": "yes",
                }
            )
        ],
    )

    assert assessment.status == "needs_review"
    assert "osm_water_crossing_requires_review" in assessment.warnings
    assert "terrain_access_not_independently_verified" not in assessment.warnings


def test_clean_osm_tags_keep_verified_with_warnings_not_full_verified() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
        stops=[_stop(osm_tags={"access": "yes", "foot": "yes", "highway": "footway"})],
    )

    assert assessment.status == "verified_with_warnings"
    assert "terrain_access_not_independently_verified" not in assessment.warnings
    assert "osm_access_forbidden" not in assessment.warnings


def test_car_route_rejects_impassable_osm_smoothness() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="car",
        stops=[_stop(osm_tags={"smoothness": "impassable", "access": "yes"})],
    )

    assert assessment.status == "unusable"
    assert "osm_surface_impassable" in assessment.warnings


def test_access_transport_mismatch_needs_review() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="car",
        stops=[_stop(access_transport=("walk", "foot"), osm_tags={"access": "yes"})],
    )

    assert assessment.status == "needs_review"
    assert "stop_access_transport_mismatch" in assessment.warnings


def test_terrain_features_unavailable_is_a_warning_not_a_review() -> None:
    assessment = assess_route_quality(
        _routing(),
        transport_mode="car",
        terrain_features=(),
    )

    assert assessment.status == "verified_with_warnings"
    assert "route_terrain_features_unavailable" in assessment.warnings


def test_route_crossing_coastline_without_ferry_needs_review() -> None:
    # Vertical coastline line crossing the default diagonal route geometry
    # (LINESTRING(34.1 44.5, 34.11 44.51, ...)) near its first segment.
    coastline = TerrainFeatureSignal(
        kind="coastline",
        points=((34.105, 44.4), (34.105, 44.6)),
    )
    assessment = assess_route_quality(
        _routing(),
        transport_mode="car",
        terrain_features=(coastline,),
    )

    assert assessment.status == "needs_review"
    assert "route_crosses_coastline_without_ferry" in assessment.warnings


def test_route_crossing_coastline_with_ferry_road_type_is_not_flagged() -> None:
    coastline = TerrainFeatureSignal(
        kind="coastline",
        points=((34.105, 44.4), (34.105, 44.6)),
    )
    assessment = assess_route_quality(
        _routing(road_types=("ferry",)),
        transport_mode="car",
        terrain_features=(coastline,),
    )

    assert "route_crosses_coastline_without_ferry" not in assessment.warnings


def test_walk_route_far_from_known_trail_warns() -> None:
    far_trail = TerrainFeatureSignal(
        kind="trail",
        points=((30.0, 40.0), (30.01, 40.01)),
    )
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
        terrain_features=(far_trail,),
    )

    assert "route_segment_far_from_known_trail" in assessment.warnings
    # A warning, not a hard failure or review downgrade on its own.
    assert assessment.usable_for_private_draft is True


def test_walk_route_on_known_trail_has_no_distance_warning() -> None:
    on_route_trail = TerrainFeatureSignal(
        kind="trail",
        points=((34.1, 44.5), (34.11, 44.51), (34.12, 44.52), (34.13, 44.53)),
    )
    assessment = assess_route_quality(
        _routing(),
        transport_mode="walk",
        pace="calm",
        terrain_features=(on_route_trail,),
    )

    assert "route_segment_far_from_known_trail" not in assessment.warnings
