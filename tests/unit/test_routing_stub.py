"""Unit tests for StubRoutingProvider (ADR-004)."""

from __future__ import annotations

import pytest

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingConstraints,
    RoutingError,
)
from tourism_backend.modules.route_builder.infrastructure.routing_stub import (
    StubRoutingProvider,
)


def _wp(lng: float, lat: float) -> RouteWaypoint:
    return RouteWaypoint(lng=lng, lat=lat)


@pytest.mark.asyncio
async def test_stub_routes_nearby_walk_points() -> None:
    provider = StubRoutingProvider()
    # ~1.1 km apart in Yalta area
    result = await provider.route(
        waypoints=[_wp(34.15, 44.49), _wp(34.16, 44.50)],
        transport_mode="walk",
    )
    assert result.provider == "stub"
    assert result.synthetic is True
    assert result.total_distance_meters > 0
    assert result.total_duration_seconds > 0
    assert len(result.legs) == 1
    assert "synthetic_straight_line" in result.warnings


@pytest.mark.asyncio
async def test_stub_rejects_unreachable_walk_leg() -> None:
    provider = StubRoutingProvider()
    with pytest.raises(RoutingError) as exc:
        await provider.route(
            waypoints=[_wp(34.1, 44.5), _wp(35.5, 45.5)],  # ~150+ km road-estimate
            transport_mode="walk",
        )
    assert exc.value.code == "routing_unreachable"


@pytest.mark.asyncio
async def test_stub_rejects_route_too_long() -> None:
    provider = StubRoutingProvider()
    with pytest.raises(RoutingError) as exc:
        await provider.route(
            waypoints=[_wp(34.15, 44.49), _wp(34.16, 44.50)],
            transport_mode="car",
            constraints=RoutingConstraints(max_total_meters=100),
        )
    assert exc.value.code == "route_too_long"


@pytest.mark.asyncio
async def test_stub_requires_two_waypoints() -> None:
    provider = StubRoutingProvider()
    with pytest.raises(RoutingError) as exc:
        await provider.route(waypoints=[_wp(34.0, 44.0)], transport_mode="car")
    assert exc.value.code == "routing_provider_error"
