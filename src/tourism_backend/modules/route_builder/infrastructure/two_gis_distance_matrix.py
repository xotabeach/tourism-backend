"""2GIS Distance Matrix API adapter (Workstream A).

Unlike the TSP/VRP endpoint, this one is a plain synchronous POST — same
base URL and status-code vocabulary as ``two_gis_routing.py``'s Routing API
(``OK``/``ROUTE_NOT_FOUND``/... per pair), just billed as
sources x targets instead of per leg. A per-pair failure (e.g. a target
2GIS's road graph can't reach) is not fatal to the whole call — that pair's
distance/duration simply comes back as ``None``, letting the caller degrade
gracefully instead of losing every other pair's real data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from math import isfinite
from typing import Any

import httpx

from tourism_backend.modules.route_builder.application.distance_matrix import (
    DistanceMatrixEntry,
    DistanceMatrixError,
    DistanceMatrixResult,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)

_MATRIX_PATH = "/get_dist_matrix"
_TRANSPORTS: dict[TransportMode, str] = {"walk": "walking", "car": "driving"}
# 2GIS's synchronous mode caps at 25 source-or-target points; keep a wide
# margin since our callers pass small, already-bounded candidate lists.
_MAX_POINTS = 25

_logger = logging.getLogger("tourism_backend.two_gis_distance_matrix")

_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.3
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60.0
_DAILY_SECONDS = 86_400.0
_OK_STATUSES = frozenset({"OK"})


class _TwoGisMatrixCircuitBreaker:
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


class _TwoGisMatrixStats:
    def __init__(self) -> None:
        self.calls_total = 0
        self.failures_total = 0
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
                "two_gis_distance_matrix_daily_budget_exceeded",
                extra={"daily_calls": self._daily_calls, "budget": budget},
            )

    def snapshot(self) -> dict[str, int]:
        return {
            "calls_total": self.calls_total,
            "failures_total": self.failures_total,
            "circuit_open_rejections": self.circuit_open_rejections,
            "quota_errors_total": self.quota_errors_total,
            "daily_calls": self._daily_calls,
        }


_circuit = _TwoGisMatrixCircuitBreaker()
_stats = _TwoGisMatrixStats()


def two_gis_distance_matrix_stats() -> dict[str, object]:
    snapshot: dict[str, object] = dict(_stats.snapshot())
    snapshot["circuit_state"] = _circuit.state
    return snapshot


def reset_two_gis_distance_matrix_state_for_tests() -> None:
    global _circuit, _stats
    _circuit = _TwoGisMatrixCircuitBreaker()
    _stats = _TwoGisMatrixStats()


def _http_error_code(response: httpx.Response) -> str:
    if response.status_code == 429:
        return "distance_matrix_quota_exceeded"
    return "distance_matrix_provider_error"


class TwoGisDistanceMatrixProvider:
    """Batched distance/duration lookups through 2GIS's synchronous matrix API.

    ``client`` is injectable for tests; production calls create a
    short-lived client per request, same lifecycle rule as the other 2GIS
    adapters in this module.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://routing.api.2gis.com",
        timeout_seconds: float = 10,
        daily_call_budget: int = 300,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("2GIS API key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._daily_call_budget = daily_call_budget
        self._client = client

    async def compute(
        self,
        *,
        sources: list[RouteWaypoint],
        targets: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> DistanceMatrixResult:
        if not sources or not targets:
            return DistanceMatrixResult(entries=())
        all_points = [*sources, *targets]
        if len(all_points) > _MAX_POINTS:
            raise DistanceMatrixError(
                "distance_matrix_too_many_points",
                f"Distance matrix supports at most {_MAX_POINTS} combined points, "
                f"got {len(all_points)}",
            )
        for point in all_points:
            if (
                not isfinite(point.lng)
                or not isfinite(point.lat)
                or not -180 <= point.lng <= 180
                or not -90 <= point.lat <= 90
            ):
                raise DistanceMatrixError(
                    "distance_matrix_request_invalid", "Waypoint coordinates are invalid"
                )

        transport = _TRANSPORTS.get(transport_mode, "walking")
        source_indices = list(range(len(sources)))
        target_indices = list(range(len(sources), len(all_points)))
        payload: dict[str, Any] = {
            "points": [{"lon": point.lng, "lat": point.lat} for point in all_points],
            "sources": source_indices,
            "targets": target_indices,
            "transport": transport,
        }
        if transport_mode == "car":
            payload["type"] = "jam"

        response = await self._post(payload)
        try:
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise DistanceMatrixError(
                _http_error_code(exc.response), "2GIS distance matrix request failed"
            ) from exc
        except ValueError as exc:
            raise DistanceMatrixError(
                "distance_matrix_provider_error", "2GIS distance matrix returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise DistanceMatrixError(
                "distance_matrix_provider_error",
                "2GIS distance matrix returned an invalid response",
            )
        routes = data.get("routes")
        if not isinstance(routes, list):
            raise DistanceMatrixError(
                "distance_matrix_provider_error", "2GIS distance matrix response has no routes"
            )
        entries: list[DistanceMatrixEntry] = []
        for row in routes:
            if not isinstance(row, dict):
                continue
            source_id = row.get("source_id")
            target_id = row.get("target_id")
            if not isinstance(source_id, int) or not isinstance(target_id, int):
                continue
            # source_id/target_id index into the combined `points` array we
            # sent, not into `sources`/`targets` — translate back.
            source_index = source_id
            target_index = target_id - len(sources)
            if not (0 <= source_index < len(sources) and 0 <= target_index < len(targets)):
                continue
            status = str(row.get("status") or "")
            distance = row.get("distance")
            duration = row.get("duration")
            ok = status in _OK_STATUSES
            entries.append(
                DistanceMatrixEntry(
                    source_index=source_index,
                    target_index=target_index,
                    distance_meters=(
                        int(distance) if ok and isinstance(distance, (int, float)) else None
                    ),
                    duration_seconds=(
                        int(duration) if ok and isinstance(duration, (int, float)) else None
                    ),
                )
            )
        return DistanceMatrixResult(entries=tuple(entries))

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        if _circuit.is_open():
            _stats.circuit_open_rejections += 1
            raise DistanceMatrixError(
                "distance_matrix_circuit_open",
                "2GIS distance matrix temporarily disabled after repeated failures",
            )
        _stats.calls_total += 1
        _stats.record_daily_call(self._daily_call_budget)

        last_error: httpx.HTTPError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._send(payload)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt + 1 < _RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                _circuit.record_failure()
                _stats.failures_total += 1
                raise DistanceMatrixError(
                    "distance_matrix_timeout", "2GIS distance matrix timed out"
                ) from exc
            else:
                _circuit.record_success()
                if response.status_code == 429:
                    _stats.quota_errors_total += 1
                return response
        if last_error is None:
            raise RuntimeError("2GIS distance matrix retry loop exited without a result")
        raise last_error

    async def _send(self, payload: dict[str, Any]) -> httpx.Response:
        params = {"key": self._api_key, "version": "2.0"}
        if self._client is not None:
            return await self._client.post(
                f"{self._base_url}{_MATRIX_PATH}", params=params, json=payload
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(f"{self._base_url}{_MATRIX_PATH}", params=params, json=payload)
