"""Valhalla adapter: parsing, legs, elevation and error mapping (spec 12a)."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingConstraints,
    RoutingError,
)
from tourism_backend.modules.route_builder.infrastructure.valhalla_routing import (
    ValhallaRoutingProvider,
    decode_polyline6,
)


def _encode(points: list[tuple[float, float]]) -> str:
    """Polyline6 encoder for (lng, lat) points, the inverse of the adapter's."""
    out = []
    last_lat = last_lng = 0
    for lng, lat in points:
        ilat, ilng = round(lat * 1e6), round(lng * 1e6)
        for delta in (ilat - last_lat, ilng - last_lng):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        last_lat, last_lng = ilat, ilng
    return "".join(out)


_A = (34.0556, 44.4197)
_B = (34.1436, 44.4678)
_C = (34.1235, 44.4307)
_WAYPOINTS = [RouteWaypoint(*_A), RouteWaypoint(*_B), RouteWaypoint(*_C)]


def _leg(points, km, seconds, elevation):
    return {
        "shape": _encode(points),
        "summary": {"length": km, "time": seconds},
        "elevation": elevation,
    }


def _provider(handler) -> tuple[ValhallaRoutingProvider, list[dict]]:
    seen: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return ValhallaRoutingProvider(
        base_url="http://valhalla:8002/", timeout_seconds=5, client=client
    ), seen


def test_polyline6_round_trip():
    points = [(34.0556, 44.4197), (34.100001, 44.44), (33.9, 44.3)]
    assert decode_polyline6(_encode(points)) == pytest.approx(points)


@pytest.mark.asyncio
async def test_walk_route_keeps_one_leg_per_pair_and_elevation():
    mid = (34.09, 44.44)
    body = {
        "trip": {
            "legs": [
                _leg([_A, mid, _B], 13.387, 5900, [10, 40, 80, 120, 100]),
                _leg([_B, _C], 6.289, 4860, [100, 60, 20]),
            ]
        }
    }
    provider, seen = _provider(lambda _: httpx.Response(200, json=body))

    result = await provider.route(waypoints=_WAYPOINTS, transport_mode="walk")

    assert seen[0]["costing"] == "pedestrian"
    assert seen[0]["costing_options"]["pedestrian"]["max_hiking_difficulty"] == 4
    assert seen[0]["costing_options"]["pedestrian"]["use_ferry"] == 0
    assert all(loc["minimum_reachability"] == 200 for loc in seen[0]["locations"])
    assert all("search_filter" not in loc for loc in seen[0]["locations"])
    assert result.provider == "valhalla"
    assert not result.synthetic
    assert [(leg.from_index, leg.to_index) for leg in result.legs] == [(0, 1), (1, 2)]
    assert [leg.distance_meters for leg in result.legs] == [13387, 6289]
    assert result.total_distance_meters == 19676
    assert result.total_duration_seconds == 10760
    # The joint point B appears once in the whole line.
    assert result.geometry_wkt is not None
    assert result.geometry_wkt.count("34.143600 44.467800") == 1
    assert result.min_altitude_meters == 10
    assert result.max_altitude_meters == 120
    assert (result.elevation_gain_meters or 0) > 0
    assert (result.elevation_loss_meters or 0) > 0


@pytest.mark.asyncio
async def test_car_uses_auto_costing_without_ferries():
    body = {"trip": {"legs": [_leg([_A, _B], 14.7, 800, []), _leg([_B, _C], 8.6, 600, [])]}}
    provider, seen = _provider(lambda _: httpx.Response(200, json=body))
    result = await provider.route(waypoints=_WAYPOINTS, transport_mode="car")
    assert seen[0]["costing"] == "auto"
    assert seen[0]["costing_options"] == {"auto": {"use_ferry": 0}}
    assert all(
        loc["search_filter"] == {"min_road_class": "residential"} for loc in seen[0]["locations"]
    )
    assert result.elevation_gain_meters is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (
            400,
            {"error_code": 442, "error": "No path could be found for input"},
            "routing_unreachable",
        ),
        (
            400,
            {"error_code": 171, "error": "No suitable edges near location"},
            "routing_unreachable",
        ),
        (500, {"error_code": 999, "error": "boom"}, "routing_provider_error"),
    ],
)
async def test_errors_map_to_routing_codes(status, payload, code):
    provider, _ = _provider(lambda _: httpx.Response(status, json=payload))
    with pytest.raises(RoutingError) as caught:
        await provider.route(waypoints=_WAYPOINTS, transport_mode="walk")
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_timeout_and_unsupported_mode():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider, _ = _provider(slow)
    with pytest.raises(RoutingError) as caught:
        await provider.route(waypoints=_WAYPOINTS, transport_mode="walk")
    assert caught.value.code == "routing_timeout"
    with pytest.raises(RoutingError) as caught:
        await provider.route(waypoints=_WAYPOINTS, transport_mode="public")
    assert caught.value.code == "routing_unsupported_mode"


@pytest.mark.asyncio
async def test_length_limits():
    body = {"trip": {"legs": [_leg([_A, _B], 30.0, 20000, []), _leg([_B, _C], 1.0, 600, [])]}}
    provider, _ = _provider(lambda _: httpx.Response(200, json=body))
    with pytest.raises(RoutingError) as caught:
        await provider.route(waypoints=_WAYPOINTS, transport_mode="walk")
    assert caught.value.code == "routing_unreachable"  # walk leg over 25 km
    with pytest.raises(RoutingError) as caught:
        await provider.route(
            waypoints=_WAYPOINTS,
            transport_mode="car",
            constraints=RoutingConstraints(max_total_meters=10_000),
        )
    assert caught.value.code == "route_too_long"
