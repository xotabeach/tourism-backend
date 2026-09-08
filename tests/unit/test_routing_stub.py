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


async def test_draft_preview_cache_round_trips_and_expires_into_a_miss() -> None:
    """The raster is addressed by id, so a lost cache entry must read as a
    miss (404 for the caller) rather than a crash or a half-drawn map."""
    import json

    from tourism_backend.modules.routes.application.service import draft_preview_shape

    line = [(34.10, 44.39), (34.08, 44.42), (34.05, 44.45)]
    stops = [(34.10, 44.39), (34.05, 44.45)]

    class _Redis:
        def __init__(self, raw: object) -> None:
            self._raw = raw

        async def get(self, key: str) -> object:
            return self._raw

    stored = json.dumps({"line": line, "stops": stops})
    assert await draft_preview_shape(_Redis(stored), "id") == (line, stops)

    # Expired entry, no Redis at all, and a corrupt payload all behave alike.
    assert await draft_preview_shape(_Redis(None), "id") is None
    assert await draft_preview_shape(None, "id") is None
    assert await draft_preview_shape(_Redis("{not json"), "id") is None
    # A line that cannot be drawn is a miss too, not a one-point map.
    thin = json.dumps({"line": [[34.1, 44.3]], "stops": stops})
    assert await draft_preview_shape(_Redis(thin), "id") is None
