"""Contract tests for the 2GIS TSP/VRP adapter (Workstream A)."""

from __future__ import annotations

import json

import httpx
import pytest

from tourism_backend.config import Settings, validate_settings
from tourism_backend.modules.route_builder.application.routing import RouteWaypoint
from tourism_backend.modules.route_builder.application.tsp import TspError
from tourism_backend.modules.route_builder.infrastructure.two_gis_tsp import (
    TwoGisTspProvider,
    reset_two_gis_tsp_state_for_tests,
)


def test_tsp_provider_2gis_requires_the_http_key() -> None:
    # _env_file=None: isolate from a real developer .env, which may already
    # define TWO_GIS_HTTP_API_KEY and would otherwise mask this check.
    with pytest.raises(RuntimeError, match="TWO_GIS_HTTP_API_KEY"):
        validate_settings(Settings(_env_file=None, tsp_provider="2gis"))  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _reset_two_gis_tsp_state() -> None:
    reset_two_gis_tsp_state_for_tests()


def _waypoints(count: int = 3) -> list[RouteWaypoint]:
    return [
        RouteWaypoint(lng=34.10 + index * 0.01, lat=44.50 + index * 0.01) for index in range(count)
    ]


@pytest.mark.asyncio
async def test_two_points_are_never_sent_to_the_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(api_key="test-secret", client=client)
        result = await provider.optimize_order(waypoints=_waypoints(2), transport_mode="walk")

    assert result.optimized is False
    assert result.order == (0, 1)


@pytest.mark.asyncio
async def test_create_then_poll_returns_optimized_order_anchored_at_start() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            assert request.url.path == "/logistics/vrp/2.0/create"
            assert request.url.params["key"] == "test-secret"
            body = json.loads(await request.aread())
            assert [wp["id"] for wp in body["waypoints"]] == ["0", "1", "2"]
            assert body["agents"] == [{"id": "agent-0", "start_at": "0"}]
            assert body["options"]["routing_type"] == "walking"
            return httpx.Response(201, json={"task_id": "task-1", "status": {"status": "Run"}})
        assert request.url.path == "/logistics/vrp/2.0/status"
        assert request.url.params["task_id"] == "task-1"
        return httpx.Response(
            200,
            json={
                "task_id": "task-1",
                "status": {"status": "Done"},
                "result": {
                    "routes": [
                        {
                            "agent_id": "agent-0",
                            "route": [
                                {"node": {"type": "waypoint", "value": {"waypoint_id": "2"}}},
                                {"node": {"type": "waypoint", "value": {"waypoint_id": "1"}}},
                            ],
                        }
                    ],
                    "dropped_waypoints": [],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(
            api_key="test-secret",
            client=client,
            poll_interval_seconds=0,
        )
        result = await provider.optimize_order(waypoints=_waypoints(3), transport_mode="walk")

    assert calls == ["POST", "GET"]
    assert result.optimized is True
    assert result.order == (0, 2, 1)


@pytest.mark.asyncio
async def test_pending_status_is_polled_until_done() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"status": {"status": "Run"}}),
            httpx.Response(200, json={"status": {"status": "Run"}}),
            httpx.Response(
                200,
                json={
                    "status": {"status": "Done"},
                    "result": {
                        "routes": [
                            {
                                "route": [
                                    {"node": {"value": {"waypoint_id": "1"}}},
                                    {"node": {"value": {"waypoint_id": "2"}}},
                                ]
                            }
                        ]
                    },
                },
            ),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"task_id": "task-1", "status": {"status": "Run"}})
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(
            api_key="test-secret",
            client=client,
            poll_interval_seconds=0,
            max_wait_seconds=5,
        )
        result = await provider.optimize_order(waypoints=_waypoints(3), transport_mode="car")

    assert result.order == (0, 1, 2)


@pytest.mark.asyncio
async def test_failed_task_raises_typed_error_without_reordering() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"task_id": "task-1", "status": {"status": "Run"}})
        return httpx.Response(200, json={"status": {"status": "Fail"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(api_key="test-secret", client=client, poll_interval_seconds=0)
        with pytest.raises(TspError) as error:
            await provider.optimize_order(waypoints=_waypoints(3), transport_mode="walk")

    assert error.value.code == "tsp_provider_error"


@pytest.mark.asyncio
async def test_timeout_budget_raises_tsp_timeout_and_stops_polling() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"task_id": "task-1", "status": {"status": "Run"}})
        return httpx.Response(200, json={"status": {"status": "Run"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(
            api_key="test-secret",
            client=client,
            poll_interval_seconds=0,
            max_wait_seconds=0,
        )
        with pytest.raises(TspError) as error:
            await provider.optimize_order(waypoints=_waypoints(3), transport_mode="walk")

    assert error.value.code == "tsp_timeout"


@pytest.mark.asyncio
async def test_quota_response_maps_to_typed_quota_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many requests"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(api_key="test-secret", client=client)
        with pytest.raises(TspError) as error:
            await provider.optimize_order(waypoints=_waypoints(3), transport_mode="walk")

    assert error.value.code == "tsp_quota_exceeded"


@pytest.mark.asyncio
async def test_dropped_waypoint_raises_instead_of_silently_reordering() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"task_id": "task-1", "status": {"status": "Run"}})
        return httpx.Response(
            200,
            json={
                "status": {"status": "Done"},
                "result": {
                    "routes": [{"route": [{"node": {"value": {"waypoint_id": "1"}}}]}],
                    "dropped_waypoints": [{"waypoint_id": "2"}],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(api_key="test-secret", client=client, poll_interval_seconds=0)
        with pytest.raises(TspError) as error:
            await provider.optimize_order(waypoints=_waypoints(3), transport_mode="walk")

    assert error.value.code == "tsp_incomplete_solution"


@pytest.mark.asyncio
async def test_too_many_points_is_rejected_before_any_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TwoGisTspProvider(api_key="test-secret", client=client)
        with pytest.raises(TspError) as error:
            await provider.optimize_order(waypoints=_waypoints(16), transport_mode="walk")

    assert error.value.code == "tsp_too_many_points"
