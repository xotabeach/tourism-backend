"""BACKEND-41: what an author's route is checked for before review."""

from datetime import UTC, datetime
from types import SimpleNamespace

from tourism_backend.modules.admin.presentation.formatters import format_route_checks
from tourism_backend.modules.routes.application.submit_checks import (
    SubmitFacts,
    check_submission,
)

YALTA = (34.1663, 44.4952)
ALUPKA = (34.0617, 44.4180)
KERCH = (36.4700, 45.3500)
TARKHANKUT = (32.5000, 45.3500)


def _facts(**overrides: object) -> SubmitFacts:
    base: dict[str, object] = {
        "transport": "walk",
        "distance_meters": 14_000,
        "movement_seconds": 4 * 3600,
        "elevation_gain_meters": 320,
        "day_count": 1,
        "line_drawn": True,
        "real_router": True,
        "has_author_points": False,
        "region_count": 1,
        "stop_points": (YALTA, ALUPKA),
        "difficulty_level": 2,
    }
    base.update(overrides)
    return SubmitFacts(**base)  # type: ignore[arg-type]


def _codes(items: list) -> list[str]:  # type: ignore[type-arg]
    return [item.code for item in items]


def test_an_ordinary_walk_passes_with_its_figures() -> None:
    report = check_submission(_facts())
    assert report.passed
    assert report.warnings == []
    assert report.metrics["distance_km"] == 14.0
    assert report.metrics["movement_hours"] == 4.0
    assert report.metrics["elevation_gain_meters"] == 320
    assert report.metrics["difficulty_level"] == 2
    assert report.metrics["line_drawn"] is True


def test_a_169_km_walk_in_one_day_is_refused() -> None:
    # A published route on prod before this check: «а это второй маршрут».
    report = check_submission(
        _facts(distance_meters=169_000, movement_seconds=35 * 3600, stop_points=(YALTA, KERCH))
    )
    assert not report.passed
    assert _codes(report.blockers) == ["route_too_long", "route_too_long_time"]
    assert "169 км за день" in report.blockers[0].message
    assert "route_stops_far_apart" in _codes(report.warnings)


def test_the_same_length_over_enough_days_only_warns() -> None:
    report = check_submission(
        _facts(distance_meters=90_000, movement_seconds=24 * 3600, day_count=3)
    )
    assert report.passed
    assert _codes(report.warnings) == ["route_long_day"]


def test_driving_has_its_own_limits() -> None:
    long_drive = _facts(transport="car", distance_meters=300_000, movement_seconds=5 * 3600)
    assert check_submission(long_drive).passed
    too_long = _facts(transport="car", distance_meters=1_000_000, movement_seconds=17 * 3600)
    assert _codes(check_submission(too_long).blockers) == [
        "route_too_long",
        "route_too_long_time",
    ]


def test_a_straight_line_is_refused_only_with_a_real_router_or_own_points() -> None:
    straight = _facts(line_drawn=False, distance_meters=None, movement_seconds=None)
    assert _codes(check_submission(straight).blockers) == ["route_line_required"]
    local = _facts(line_drawn=False, real_router=False)
    assert check_submission(local).passed
    assert "route_line_synthetic" in _codes(check_submission(local).warnings)
    own_points = _facts(line_drawn=False, real_router=False, has_author_points=True)
    assert _codes(check_submission(own_points).blockers) == ["route_line_required"]


def test_with_no_length_from_the_router_it_is_estimated_by_the_straight_line() -> None:
    report = check_submission(
        _facts(
            line_drawn=False,
            distance_meters=None,
            movement_seconds=None,
            stop_points=(KERCH, TARKHANKUT),
        )
    )
    # Too long comes first: that is what the author can fix.
    assert _codes(report.blockers) == ["route_too_long", "route_line_required"]
    assert report.metrics["distance_estimated"] is True
    assert "route_distance_unknown" in _codes(report.warnings)


def test_mixed_regions_and_scattered_stops() -> None:
    assert _codes(check_submission(_facts(region_count=2)).blockers) == ["invalid_route_region"]
    scattered = check_submission(
        _facts(distance_meters=20_000, stop_points=(YALTA, ALUPKA, (34.4, 44.9), YALTA))
    )
    assert scattered.passed
    assert {"route_stops_far_apart", "route_stops_spread"} <= set(_codes(scattered.warnings))


def test_a_very_short_route_and_router_warnings_reach_the_moderator() -> None:
    report = check_submission(
        _facts(
            distance_meters=120,
            router_warnings=("stairs_require_review", "something_new"),
            road_types=("stairs", "footway"),
        )
    )
    assert report.passed
    assert _codes(report.warnings) == [
        "route_very_short",
        "router:stairs_require_review",
        "router:something_new",
    ]
    assert report.warnings[1].message == "Есть лестницы"
    assert report.metrics["road_types"] == ["footway", "stairs"]


def test_the_admin_shows_the_stored_report() -> None:
    report = check_submission(_facts(distance_meters=30_000))
    route = SimpleNamespace(
        accessibility={"submit_check": report.as_json(checked_at=datetime.now(UTC))},
        difficulty_level=2,
    )
    html = str(format_route_checks(route, "accessibility"))
    assert "прошёл" in html
    assert "30.0 км" in html
    assert "30 км за день пешком" in html
    assert "Все данные" in html


def test_the_admin_shows_router_figures_without_a_check() -> None:
    route = SimpleNamespace(
        accessibility={
            "routing": {
                "synthetic": False,
                "distance_meters": 12_400,
                "movement_duration_seconds": 3 * 3600,
                "elevation_gain_meters": 410,
                "transport_mode": "walk",
            }
        },
        difficulty_level=3,
    )
    html = str(format_route_checks(route, "accessibility"))
    assert "Проверки при отправке не было" in html
    assert "12.4 км" in html
    assert "410 м" in html
    assert "по дорогам и тропам" in html
    assert "<script>" not in str(
        format_route_checks(SimpleNamespace(accessibility={"x": "<script>"}), "accessibility")
    )
