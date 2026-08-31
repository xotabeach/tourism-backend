"""Contract tests for the 2GIS Distance Matrix adapter (Workstream A)."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.config import Settings, validate_settings
from tourism_backend.modules.route_builder.application.distance_matrix import (
    DistanceMatrixError,
)
from tourism_backend.modules.route_builder.application.routing import RouteWaypoint
from tourism_backend.modules.route_builder.infrastructure.two_gis_distance_matrix import (
    TwoGisDistanceMatrixProvider,
    reset_two_gis_distance_matrix_state_for_tests,
)


def test_distance_matrix_provider_2gis_requires_the_http_key() -> None:
    # _env_file=None: isolate from a real developer .env, which may already
    # define TWO_GIS_HTTP_API_KEY and would otherwise mask this check.
    with pytest.raises(RuntimeError, match="TWO_GIS_HTTP_API_KEY"):
        validate_settings(Settings(_env_file=None, distance_matrix_provider="2gis"))  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_two_gis_distance_matrix_state_for_tests()


def _source() -> list[RouteWaypoint]:
    return [RouteWaypoint(lng=34.10, lat=44.50)]


def _targets(count: int = 2) -> list[RouteWaypoint]:
    return [
        RouteWaypoint(lng=34.11 + index * 0.01, lat=44.51 + index * 0.01) for index in range(count)
    ]


@pytest.mark.asyncio
async def test_matrix_request_indexes_points_and_parses_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/get_dist_matrix"
        assert request.url.params["key"] == "test-secret"
        body = json.loads(await request.aread())
        assert body["sources"] == [0]
        assert body["targets"] == [1, 2]
        assert len(body["points"]) == 3
        assert body["transport"] == "walking"
        return httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "status": "OK",
                        "source_id": 0,
                        "target_id": 1,
                        "distance": 500,
                        "duration": 300,
                    },
                    {
                        "status": "OK",
                        "source_id": 0,
                        "target_id": 2,
                        "distance": 900,
                        "duration": 640,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisDistanceMatrixProvider(api_key="test-secret", client=client)
        result = await provider.compute(
            sources=_source(), targets=_targets(), transport_mode="walk"
        )

    assert result.distance_meters(source_index=0, target_index=0) == 500
    assert result.duration_seconds(source_index=0, target_index=0) == 300
    assert result.distance_meters(source_index=0, target_index=1) == 900
    assert result.duration_seconds(source_index=0, target_index=1) == 640


@pytest.mark.asyncio
async def test_unreachable_pair_comes_back_as_none_not_a_fabricated_number() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "routes": [
                    {"status": "ROUTE_NOT_FOUND", "source_id": 0, "target_id": 1},
                    {
                        "status": "OK",
                        "source_id": 0,
                        "target_id": 2,
                        "distance": 900,
                        "duration": 640,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisDistanceMatrixProvider(api_key="test-secret", client=client)
        result = await provider.compute(
            sources=_source(), targets=_targets(), transport_mode="walk"
        )

    assert result.distance_meters(source_index=0, target_index=0) is None
    assert result.duration_seconds(source_index=0, target_index=0) is None
    assert result.distance_meters(source_index=0, target_index=1) == 900


@pytest.mark.asyncio
async def test_empty_sources_or_targets_never_calls_the_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisDistanceMatrixProvider(api_key="test-secret", client=client)
        result = await provider.compute(sources=[], targets=_targets(), transport_mode="walk")

    assert result.entries == ()


@pytest.mark.asyncio
async def test_quota_response_maps_to_typed_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many requests"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisDistanceMatrixProvider(api_key="test-secret", client=client)
        with pytest.raises(DistanceMatrixError) as error:
            await provider.compute(sources=_source(), targets=_targets(), transport_mode="walk")

    assert error.value.code == "distance_matrix_quota_exceeded"


@pytest.mark.asyncio
async def test_too_many_combined_points_is_rejected_before_any_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisDistanceMatrixProvider(api_key="test-secret", client=client)
        with pytest.raises(DistanceMatrixError) as error:
            await provider.compute(sources=_source(), targets=_targets(30), transport_mode="walk")

    assert error.value.code == "distance_matrix_too_many_points"
