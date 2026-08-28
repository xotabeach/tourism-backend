"""Route-detail routing metadata normalization tests."""

from tourism_backend.modules.routes.application.service import _routing_for_route


def test_routing_metadata_is_normalized_for_public_contract() -> None:
    value = {
        "routing": {
            "provider": "2gis",
            "synthetic": False,
            "quality_status": "needs_review",
            "quality_policy_version": "v1",
            "warnings": ["ferry_schedule_and_access_unknown", 42],
            "movement_duration_seconds": 600,
            "visit_duration_minutes": 90,
            "transfer_duration_seconds": 0,
            "buffer_duration_seconds": 0,
            "total_duration_seconds": 6_000,
            "elevation_gain_meters": 120,
            "elevation_loss_meters": 80,
            "min_altitude_meters": -3,
            "max_altitude_meters": 430,
            "max_road_angle_degrees": 17.5,
            "road_types": ["ferry", None],
        }
    }

    result = _routing_for_route(value)

    assert result is not None
    assert result.provider == "2gis"
    assert result.quality_status == "needs_review"
    assert result.quality_policy_version == "v1"
    assert result.warnings == ["ferry_schedule_and_access_unknown"]
    assert result.movement_duration_seconds == 600
    assert result.visit_duration_minutes == 90
    assert result.total_duration_seconds == 6_000
    assert result.min_altitude_meters == -3
    assert result.max_altitude_meters == 430
    assert result.road_types == ["ferry"]


def test_unknown_quality_status_fails_closed_to_unknown() -> None:
    result = _routing_for_route({"routing": {"quality_status": "definitely_safe"}})

    assert result is not None
    assert result.quality_status == "unknown"
