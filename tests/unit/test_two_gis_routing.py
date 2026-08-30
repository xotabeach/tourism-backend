"""Contract tests for the 2GIS Routing API adapter."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    TwoGisRoutingProvider,
    reset_two_gis_routing_state_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_two_gis_state() -> None:
    # The retry/circuit-breaker/cache state is process-wide by design (see
    # two_gis_routing.py); without a reset, one test's failures or cached
    # route could leak into an unrelated test in the same pytest session.
    reset_two_gis_routing_state_for_tests()


def _waypoints(count: int = 2) -> list[RouteWaypoint]:
    return [
        RouteWaypoint(lng=34.10 + index * 0.01, lat=44.50 + index * 0.01) for index in range(count)
    ]


@pytest.mark.asyncio
async def test_2gis_response_is_normalized_without_leaking_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/routing/7.0.0/global"
        assert request.url.params["key"] == "test-secret"
        body = await request.aread()
        decoded = json.loads(body)
        assert decoded["transport"] == "walking"
        assert [point["type"] for point in decoded["points"]] == [
            "walking",
            "walking",
        ]
        assert "ban_stairway" in decoded["filters"]
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "type": "result",
                "result": [
                    {
                        "total_distance": 1234,
                        "total_duration": 987,
                        "filter_road_types": {"0": "dirt_road"},
                        "altitudes_info": {
                            # 2GIS altitude values are centimetres.
                            "elevation_gain": 4200,
                            "elevation_loss": 1700,
                            "min_altitude": -300,
                            "max_altitude": 12950,
                            "max_road_angle": 12,
                        },
                        "maneuvers": {
                            "0": {
                                "outcoming_path": {
                                    "geometry": {
                                        "0": {
                                            "selection": ("LINESTRING(34.1 44.5, 34.11 44.51 120)")
                                        }
                                    }
                                }
                            }
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(
            api_key="test-secret",
            client=client,
            filters=(),
        )
        result = await provider.route(waypoints=_waypoints(), transport_mode="walk")

    assert result.provider == "2gis"
    assert result.synthetic is False
    assert result.total_distance_meters == 1234
    assert result.total_duration_seconds == 987
    assert result.geometry_wkt == "LINESTRING(34.1000000 44.5000000, 34.1100000 44.5100000)"
    assert result.elevation_gain_meters == 42
    assert result.elevation_loss_meters == 17
    assert result.min_altitude_meters == -3
    assert result.max_altitude_meters == 130
    assert result.max_road_angle_degrees == 12
    assert result.road_types == ("dirt_road",)
    assert "provider_returned_filtered_road_type" in result.warnings


@pytest.mark.asyncio
async def test_2gis_status_is_mapped_to_typed_unreachable_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ROUTE_NOT_FOUND",
                "type": "error",
                "message": "no route",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        with pytest.raises(RoutingError) as error:
            await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert error.value.code == "routing_unreachable"
    assert "test-secret" not in error.value.message


@pytest.mark.asyncio
async def test_2gis_http_429_is_mapped_to_quota_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "quota"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        with pytest.raises(RoutingError) as error:
            await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert error.value.code == "routing_quota_exceeded"
    assert "test-secret" not in error.value.message


@pytest.mark.asyncio
async def test_2gis_rejects_public_transport_until_dedicated_adapter_exists() -> None:
    provider = TwoGisRoutingProvider(api_key="test-secret")
    with pytest.raises(RoutingError) as error:
        await provider.route(waypoints=_waypoints(), transport_mode="public")
    assert error.value.code == "routing_unsupported_mode"


@pytest.mark.asyncio
async def test_2gis_rejects_invalid_coordinates_before_network() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        with pytest.raises(RoutingError) as error:
            await provider.route(
                waypoints=[RouteWaypoint(lng=181, lat=44.5), _waypoints()[1]],
                transport_mode="walk",
            )

    assert error.value.code == "routing_request_invalid"
    assert calls == 0


@pytest.mark.asyncio
async def test_2gis_chunks_routes_above_provider_point_limit() -> None:
    calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        calls.append(payload)
        points = payload["points"]
        assert isinstance(points, list)
        assert len(points) <= 5
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "type": "result",
                "result": [
                    {
                        "total_distance": 100,
                        "total_duration": 60,
                        "maneuvers": [
                            {
                                "outcoming_path": {
                                    "geometry": [
                                        {"selection": ("LINESTRING(34.1 44.5, 34.11 44.51)")}
                                    ]
                                }
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(
            api_key="test-secret",
            client=client,
            filters=(),
            max_route_meters=50_000,
        )
        result = await provider.route(
            waypoints=_waypoints(6),
            transport_mode="walk",
        )

    assert len(calls) == 2
    assert [len(call["points"]) for call in calls] == [5, 2]
    assert result.total_distance_meters == 200
    assert result.total_duration_seconds == 120
    assert [leg.from_index for leg in result.legs] == [0, 4]
    assert "provider_points_chunked" in result.warnings


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "OK",
            "type": "result",
            "result": [
                {
                    "total_distance": 100,
                    "total_duration": 60,
                    "maneuvers": [
                        {
                            "outcoming_path": {
                                "geometry": [
                                    {"selection": ("LINESTRING(34.1 44.5, 34.11 44.51)")}
                                ]
                            }
                        }
                    ],
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_2gis_routing_retries_once_on_timeout_then_succeeds() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.TimeoutException("boom", request=request)
        return _success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        result = await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert calls == 2
    assert result.total_distance_meters == 100


@pytest.mark.asyncio
async def test_2gis_routing_times_out_after_retry_exhausted() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        with pytest.raises(RoutingError) as error:
            await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert error.value.code == "routing_timeout"
    assert calls == 2  # 1 initial attempt + 1 bounded retry, not unbounded


@pytest.mark.asyncio
async def test_2gis_circuit_opens_after_repeated_failures_and_fails_fast() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        for _ in range(3):
            with pytest.raises(RoutingError):
                await provider.route(waypoints=_waypoints(), transport_mode="car")
        calls_before_open = calls

        with pytest.raises(RoutingError) as error:
            await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert error.value.code == "routing_circuit_open"
    assert calls == calls_before_open  # circuit-open call never touched the network


@pytest.mark.asyncio
async def test_2gis_routing_cache_hit_skips_network_call() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _success_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisRoutingProvider(api_key="test-secret", client=client)
        first = await provider.route(waypoints=_waypoints(), transport_mode="car")
        second = await provider.route(waypoints=_waypoints(), transport_mode="car")

    assert calls == 1
    assert "provider_result_cached" not in first.warnings
    assert "provider_result_cached" in second.warnings
    assert second.total_distance_meters == first.total_distance_meters
