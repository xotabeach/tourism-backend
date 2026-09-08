from datetime import date

from tourism_backend.modules.route_builder.application.itinerary import build_trip_plan
from tourism_backend.modules.route_builder.application.routing import RouteLegResult, RoutingResult


def _routing(minutes: int) -> RoutingResult:
    return RoutingResult(
        provider="test",
        synthetic=False,
        legs=(
            RouteLegResult(
                from_index=0,
                to_index=1,
                distance_meters=5000,
                duration_seconds=minutes * 60,
                geometry_wkt=None,
            ),
        ),
        total_distance_meters=5000,
        total_duration_seconds=minutes * 60,
    )


def test_short_walk_preserves_stop_order_and_start_date() -> None:
    plan = build_trip_plan(
        stops=[("a", "Парк", 30), ("b", "Музей", 60)],
        routing=_routing(30),
        pace="calm",
        transport_mode="walk",
        with_children=False,
        start_date=date(2026, 9, 15),
    )
    assert len(plan.days) == 1
    assert plan.start_date == date(2026, 9, 15)
    assert [event.place_id for event in plan.days[0].events if event.kind == "visit"] == ["a", "b"]
    events = plan.days[0].events
    assert all(
        b.start_minute >= a.start_minute + a.duration_minutes
        for a, b in zip(events, events[1:], strict=False)
    )


def test_long_walk_requests_overnight_without_inventing_a_hotel() -> None:
    plan = build_trip_plan(
        stops=[("a", "Дворец", 180), ("b", "Парк", 120)],
        routing=_routing(120),
        pace="calm",
        transport_mode="walk",
        with_children=True,
    )
    assert len(plan.days) == 2
    overnight = plan.days[0].events[-1]
    assert overnight.kind == "overnight_needed"
    assert overnight.place_id == "a"
    assert "Выбрать ночлег" in overnight.title
    assert any(event.kind == "meal_break" for day in plan.days for event in day.events)


def test_indivisible_long_leg_is_flagged_instead_of_hidden() -> None:
    plan = build_trip_plan(
        stops=[("a", "Старт", 30), ("b", "Финиш", 30)],
        routing=_routing(600),
        pace="calm",
        transport_mode="walk",
        with_children=False,
    )
    assert any("длиннее комфортного дня" in warning for warning in plan.warnings)
    assert (
        sum(
            event.duration_minutes
            for day in plan.days
            for event in day.events
            if event.kind == "travel"
        )
        >= 600
    )
