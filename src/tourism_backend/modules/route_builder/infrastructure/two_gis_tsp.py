"""2GIS TSP/VRP API adapter (Workstream A: stop-order optimization).

2GIS's "TSP API" is the async VRP task engine, not a single synchronous
call: ``POST .../logistics/vrp/2.0/create`` returns a ``task_id`` and the
solution is fetched by polling ``GET .../logistics/vrp/2.0/status``. This
adapter uses the smallest shape that answers our question — one agent,
no depot, ``agent.start_at`` anchoring the route at the caller's first
point — and never returns to that anchor (``finish_at`` is left unset).

Solving can take several seconds and the demo-key budget for this endpoint
is small (1000 tasks/month, billed points x agents), so this must stay a
strictly best-effort, time-boxed call: ``TspProvider.optimize_order`` is
documented as fallback-safe, and every failure path here raises a typed
``TspError`` rather than blocking route generation indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import time
from math import isfinite
from typing import Any

import httpx

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)
from tourism_backend.modules.route_builder.application.tsp import (
    TspError,
    TspOrderResult,
)

_CREATE_PATH = "/logistics/vrp/2.0/create"
_STATUS_PATH = "/logistics/vrp/2.0/status"
_TRANSPORTS: dict[TransportMode, str] = {"walk": "walking", "car": "driving"}
_MAX_WAYPOINTS = 15

_logger = logging.getLogger("tourism_backend.two_gis_tsp")

# Process-wide by design, same rationale as two_gis_routing.py: a fresh
# TwoGisTspProvider is constructed on every call (see tsp_factory.py).
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.3
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60.0
_DAILY_SECONDS = 86_400.0


class _TwoGisTspCircuitBreaker:
    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return time.monotonic() - self._opened_at < _CIRCUIT_COOLDOWN_SECONDS

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD:
            self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        return "open" if self.is_open() else "closed"


class _TwoGisTspStats:
    def __init__(self) -> None:
        self.calls_total = 0
        self.failures_total = 0
        self.timeouts_total = 0
        self.circuit_open_rejections = 0
        self.quota_errors_total = 0
        self._daily_calls = 0
        self._daily_window_started = time.monotonic()
        self._daily_budget_warned = False

    def record_daily_call(self, budget: int) -> None:
        now = time.monotonic()
        if now - self._daily_window_started >= _DAILY_SECONDS:
            self._daily_calls = 0
            self._daily_window_started = now
            self._daily_budget_warned = False
        self._daily_calls += 1
        if not self._daily_budget_warned and self._daily_calls > budget:
            self._daily_budget_warned = True
            _logger.warning(
                "two_gis_tsp_daily_budget_exceeded",
                extra={"daily_calls": self._daily_calls, "budget": budget},
            )

    def snapshot(self) -> dict[str, int]:
        return {
            "calls_total": self.calls_total,
            "failures_total": self.failures_total,
            "timeouts_total": self.timeouts_total,
            "circuit_open_rejections": self.circuit_open_rejections,
            "quota_errors_total": self.quota_errors_total,
            "daily_calls": self._daily_calls,
        }


_circuit = _TwoGisTspCircuitBreaker()
_stats = _TwoGisTspStats()


def two_gis_tsp_stats() -> dict[str, object]:
    """Process-local counters + circuit state — no key, no URL, no PII."""
    snapshot: dict[str, object] = dict(_stats.snapshot())
    snapshot["circuit_state"] = _circuit.state
    return snapshot


def reset_two_gis_tsp_state_for_tests() -> None:
    global _circuit, _stats
    _circuit = _TwoGisTspCircuitBreaker()
    _stats = _TwoGisTspStats()


def _http_error_code(response: httpx.Response) -> str:
    if response.status_code in (429, 402):
        return "tsp_quota_exceeded"
    return "tsp_provider_error"


class TwoGisTspProvider:
    """Best-effort stop-order optimization through 2GIS's VRP task API.

    ``client`` is injectable for tests. Production calls create a
    short-lived client per request, same lifecycle rule as
    ``TwoGisRoutingProvider``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://routing.api.2gis.com",
        timeout_seconds: float = 10,
        poll_interval_seconds: float = 0.6,
        max_wait_seconds: float = 5.0,
        daily_call_budget: int = 200,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("2GIS API key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_wait = max_wait_seconds
        self._daily_call_budget = daily_call_budget
        self._client = client

    async def optimize_order(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> TspOrderResult:
        if len(waypoints) < 3:
            # Two points have exactly one possible order — nothing to solve.
            return TspOrderResult(order=tuple(range(len(waypoints))), optimized=False)
        if len(waypoints) > _MAX_WAYPOINTS:
            raise TspError(
                "tsp_too_many_points",
                f"TSP optimize supports at most {_MAX_WAYPOINTS} points, got {len(waypoints)}",
            )
        for point in waypoints:
            if (
                not isfinite(point.lng)
                or not isfinite(point.lat)
                or not -180 <= point.lng <= 180
                or not -90 <= point.lat <= 90
            ):
                raise TspError("tsp_request_invalid", "Waypoint coordinates are invalid")

        transport = _TRANSPORTS.get(transport_mode, "walking")
        ids = [str(index) for index in range(len(waypoints))]
        options: dict[str, Any] = {"routing_type": transport}
        if transport_mode == "car":
            options["route_type"] = "jam"
        payload: dict[str, Any] = {
            "waypoints": [
                {
                    "id": wp_id,
                    "point": {"lat": point.lat, "lon": point.lng},
                    "type": "delivery",
                }
                for wp_id, point in zip(ids, waypoints, strict=True)
            ],
            # No finish_at: the tourist does not need to walk back to the
            # first stop, unlike a delivery courier's depot round trip.
            "agents": [{"id": "agent-0", "start_at": ids[0]}],
            "options": options,
        }

        task_id = await self._create_task(payload)
        order_ids = await self._poll_until_done(task_id)
        order = [0, *(int(item) for item in order_ids)]
        if len(order) != len(waypoints) or set(order) != set(range(len(waypoints))):
            # A partial/dropped solve must never silently reorder — the
            # caller falls back to its own order instead of risking a route
            # that skips or duplicates a stop the user asked for.
            raise TspError(
                "tsp_incomplete_solution",
                "2GIS TSP dropped or duplicated a waypoint",
            )
        return TspOrderResult(order=tuple(order), optimized=True)

    async def _create_task(self, payload: dict[str, Any]) -> str:
        response = await self._post(_CREATE_PATH, payload)
        try:
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise TspError(_http_error_code(exc.response), "2GIS TSP create failed") from exc
        except ValueError as exc:
            raise TspError("tsp_provider_error", "2GIS TSP returned invalid JSON") from exc
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise TspError("tsp_provider_error", "2GIS TSP did not return a task_id")
        return task_id

    async def _poll_until_done(self, task_id: str) -> list[str]:
        deadline = time.monotonic() + self._max_wait
        while True:
            response = await self._get(_STATUS_PATH, {"task_id": task_id})
            try:
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                raise TspError(_http_error_code(exc.response), "2GIS TSP status failed") from exc
            except ValueError as exc:
                raise TspError("tsp_provider_error", "2GIS TSP returned invalid JSON") from exc
            status_block = data.get("status") if isinstance(data, dict) else None
            status = status_block.get("status") if isinstance(status_block, dict) else None
            if status == "Done":
                return self._route_order_from_status(data)
            if status in {"Fail", "Partial"}:
                raise TspError("tsp_provider_error", f"2GIS TSP task ended with status={status}")
            if time.monotonic() >= deadline:
                raise TspError("tsp_timeout", "2GIS TSP did not finish in time")
            await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _route_order_from_status(data: dict[str, Any]) -> list[str]:
        result = data.get("result") if isinstance(data, dict) else None
        routes = result.get("routes") if isinstance(result, dict) else None
        if not isinstance(routes, list) or not routes:
            raise TspError("tsp_provider_error", "2GIS TSP result has no routes")
        first_route = routes[0]
        stops = first_route.get("route") if isinstance(first_route, dict) else None
        if not isinstance(stops, list):
            raise TspError("tsp_provider_error", "2GIS TSP route has no stops")
        order: list[str] = []
        for stop in stops:
            node = stop.get("node") if isinstance(stop, dict) else None
            value = node.get("value") if isinstance(node, dict) else None
            waypoint_id = value.get("waypoint_id") if isinstance(value, dict) else None
            if isinstance(waypoint_id, str) and waypoint_id:
                order.append(waypoint_id)
        return order

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return await self._send("POST", path, json=payload, params={})

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        return await self._send("GET", path, json=None, params=params)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None,
        params: dict[str, str],
    ) -> httpx.Response:
        if _circuit.is_open():
            _stats.circuit_open_rejections += 1
            raise TspError(
                "tsp_circuit_open",
                "2GIS TSP temporarily disabled after repeated failures",
            )
        query = {"key": self._api_key, **params}
        if method == "POST":
            # Only task creation is billable; status polling is not counted
            # against the daily budget.
            _stats.calls_total += 1
            _stats.record_daily_call(self._daily_call_budget)

        last_error: httpx.HTTPError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._request(method, path, json=json, params=query)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt + 1 < _RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                _circuit.record_failure()
                _stats.failures_total += 1
                _stats.timeouts_total += 1
                raise TspError("tsp_timeout", "2GIS TSP request timed out") from exc
            else:
                _circuit.record_success()
                if response.status_code == 429:
                    _stats.quota_errors_total += 1
                return response
        if last_error is None:
            raise RuntimeError("2GIS TSP retry loop exited without a result")
        raise last_error

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None,
        params: dict[str, str],
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(
                method, f"{self._base_url}{path}", params=params, json=json
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, f"{self._base_url}{path}", params=params, json=json)
