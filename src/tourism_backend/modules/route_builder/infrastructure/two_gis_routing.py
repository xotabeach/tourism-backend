"""2GIS HTTP Routing API adapter.

The adapter deliberately implements only the application port used by the
deterministic route builder.  Provider-specific response details stay here;
the rest of the backend never receives a 2GIS response object or an API key.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from math import isfinite
from typing import Any

import httpx

from tourism_backend.modules.route_builder.application.routing import (
    RouteLegResult,
    RouteWaypoint,
    RoutingConstraints,
    RoutingError,
    RoutingResult,
    TransportMode,
    default_max_leg_meters,
)

_ROUTING_PATH = "/routing/7.0.0/global"
_MAX_WALK_POINTS = 5
_MAX_OTHER_POINTS = 10
_MAX_RESPONSE_BYTES = 8_000_000
_MAX_GEOMETRY_CHARS = 2_000_000
_MAX_GEOMETRY_POINTS = 50_000
_TRANSPORTS: dict[TransportMode, str] = {
    "walk": "walking",
    "car": "driving",
}
_WKT_LINE_RE = re.compile(r"^\s*LINESTRING(?:\s+Z)?\s*\((?P<body>.*)\)\s*$", re.I)

_logger = logging.getLogger("tourism_backend.two_gis_routing")

# Retry/circuit-breaker/cache/stats state is process-wide by design: a fresh
# TwoGisRoutingProvider is constructed on every call (see routing_factory.py),
# so a per-instance breaker would never accumulate failures across requests.
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.3
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60.0
_CACHE_TTL_SECONDS = 3600.0
_CACHE_MAX_ITEMS = 64
_DAILY_SECONDS = 86_400.0


class _TwoGisCircuitBreaker:
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


class _TwoGisStats:
    def __init__(self) -> None:
        self.calls_total = 0
        self.cache_hits = 0
        self.retries = 0
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
                "two_gis_daily_budget_exceeded",
                extra={"daily_calls": self._daily_calls, "budget": budget},
            )

    def snapshot(self) -> dict[str, int]:
        return {
            "calls_total": self.calls_total,
            "cache_hits": self.cache_hits,
            "retries": self.retries,
            "failures_total": self.failures_total,
            "circuit_open_rejections": self.circuit_open_rejections,
            "quota_errors_total": self.quota_errors_total,
            "daily_calls": self._daily_calls,
        }


_circuit = _TwoGisCircuitBreaker()
_stats = _TwoGisStats()
_route_cache: dict[tuple[Any, ...], tuple[float, RoutingResult]] = {}


def two_gis_routing_stats() -> dict[str, object]:
    """Process-local counters + circuit state — no key, no URL, no PII.

    Backs the public ``/api/v1/maps/two-gis/status`` endpoint and the tests.
    """
    snapshot: dict[str, object] = dict(_stats.snapshot())
    snapshot["circuit_state"] = _circuit.state
    return snapshot


def reset_two_gis_circuit() -> None:
    """Close the breaker again.

    Request traffic must let the breaker do its job, but a bounded ops batch
    (see scripts/backfill_route_geometry.py) walks unrelated routes: one slow
    route should not fail-fast every remaining one.
    """
    global _circuit
    _circuit = _TwoGisCircuitBreaker()


def reset_two_gis_routing_state_for_tests() -> None:
    global _stats
    reset_two_gis_circuit()
    _stats = _TwoGisStats()
    _route_cache.clear()


def _http_error_code(response: httpx.Response) -> str:
    """Map a provider HTTP failure to a typed code.

    A demo key answers an over-long route with 403 and an explanatory body
    rather than a distance field, so without this it surfaced as a generic
    provider error and looked like an outage instead of a plan limit.
    """
    if response.status_code == 429:
        return "routing_quota_exceeded"
    if response.status_code == 403:
        try:
            message = str(response.json().get("message") or "")
        except ValueError:
            message = ""
        if "excessive distance" in message.casefold():
            return "route_too_long"
    return "routing_provider_error"


def _route_cache_key(
    *,
    waypoints: list[RouteWaypoint],
    transport_mode: TransportMode,
    filters: tuple[str, ...],
    alternative: int,
) -> tuple[Any, ...]:
    points = tuple((round(point.lat, 5), round(point.lng, 5)) for point in waypoints)
    return (points, transport_mode, filters, alternative)


def _parse_linestring(value: object) -> list[str]:
    """Return normalized 2D coordinate pairs from a provider WKT string."""

    if not isinstance(value, str):
        return []
    if len(value) > _MAX_GEOMETRY_CHARS:
        return []
    match = _WKT_LINE_RE.match(value)
    if match is None:
        return []
    pairs: list[str] = []
    for raw_point in match.group("body").split(","):
        fields = raw_point.strip().split()
        if len(fields) < 2:
            continue
        try:
            # Validate values and normalize locale-independent formatting.  A
            # malformed provider point must not become invalid PostGIS WKT.
            lon = float(fields[0])
            lat = float(fields[1])
        except (TypeError, ValueError):
            continue
        if not isfinite(lon) or not isfinite(lat):
            continue
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            continue
        pairs.append(f"{lon:.7f} {lat:.7f}")
        if len(pairs) > _MAX_GEOMETRY_POINTS:
            return []
    return pairs


def _as_items(value: object) -> list[object]:
    """Normalize JSON arrays and the indexed objects used in 2GIS samples."""

    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        if "selection" in value:
            return [value]
        # The API examples serialize arrays as objects with numeric keys in
        # generated OpenAPI output. Preserve numeric ordering and ignore any
        # unexpected metadata keys.
        indexed = [
            (int(key), item)
            for key, item in value.items()
            if isinstance(key, str) and key.isdigit()
        ]
        return [item for _, item in sorted(indexed)]
    return []


def _route_geometry(result: Mapping[str, Any]) -> str | None:
    """Flatten detailed maneuver geometry into one valid LINESTRING."""

    coordinates: list[str] = []
    paths: list[object] = []
    for key in ("begin_pedestrian_path", "maneuvers", "end_pedestrian_path"):
        value = result.get(key)
        if key == "maneuvers":
            for maneuver in _as_items(value):
                if isinstance(maneuver, Mapping):
                    paths.append(maneuver.get("outcoming_path"))
        else:
            paths.append(value)
    for path in paths:
        if not isinstance(path, Mapping):
            continue
        geometries = _as_items(path.get("geometry"))
        if not geometries:
            continue
        for geometry in geometries:
            if not isinstance(geometry, Mapping):
                continue
            points = _parse_linestring(geometry.get("selection"))
            for point in points:
                if not coordinates or coordinates[-1] != point:
                    coordinates.append(point)
                    if len(coordinates) > _MAX_GEOMETRY_POINTS:
                        return None
    if len(coordinates) < 2:
        return None
    return f"LINESTRING({', '.join(coordinates)})"


def _merge_linestrings(lines: list[str]) -> str | None:
    """Join normalized WKT lines while removing the shared chunk endpoint."""

    merged: list[str] = []
    for line in lines:
        points = _parse_linestring(line)
        for point in points:
            if not merged or merged[-1] != point:
                merged.append(point)
                if len(merged) > _MAX_GEOMETRY_POINTS:
                    return None
    if len(merged) < 2:
        return None
    return f"LINESTRING({', '.join(merged)})"


def _as_non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise RoutingError("routing_provider_error", f"2GIS response misses {field}")
    parsed = int(round(value))
    if parsed < 0:
        raise RoutingError("routing_provider_error", f"2GIS response has invalid {field}")
    return parsed


class TwoGisRoutingProvider:
    """Road/path routing through the server-side 2GIS HTTP API.

    ``client`` is injectable for tests.  Production calls create a short-lived
    client per request, so connection resources are always closed even when a
    provider timeout or malformed response occurs.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://routing.api.2gis.com",
        timeout_seconds: float = 10,
        alternative: int = 0,
        max_route_meters: int | None = 50_000,
        filters: tuple[str, ...] = ("dirt_road", "ferry"),
        daily_call_budget: int = 500,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("2GIS API key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._alternative = max(0, min(alternative, 3))
        self._max_route_meters = max_route_meters
        self._filters = tuple(item.strip() for item in filters if item.strip())
        self._daily_call_budget = daily_call_budget
        self._client = client

    async def route(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None = None,
    ) -> RoutingResult:
        if len(waypoints) < 2:
            raise RoutingError("routing_provider_error", "At least two waypoints are required")
        for waypoint in waypoints:
            if (
                not isfinite(waypoint.lng)
                or not isfinite(waypoint.lat)
                or not -180 <= waypoint.lng <= 180
                or not -90 <= waypoint.lat <= 90
            ):
                raise RoutingError(
                    "routing_request_invalid",
                    "Waypoint coordinates are invalid",
                )
        transport = _TRANSPORTS.get(transport_mode)
        if transport is None:
            raise RoutingError(
                "routing_unsupported_mode",
                f"2GIS global routing does not support mode {transport_mode!r} yet",
            )

        cache_key = _route_cache_key(
            waypoints=waypoints,
            transport_mode=transport_mode,
            filters=self._filters,
            alternative=self._alternative,
        )
        now = time.monotonic()
        cached = _route_cache.get(cache_key)
        if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
            _stats.cache_hits += 1
            cached_result = cached[1]
            return replace(
                cached_result,
                warnings=(*cached_result.warnings, "provider_result_cached"),
            )

        max_points = _MAX_WALK_POINTS if transport_mode == "walk" else _MAX_OTHER_POINTS
        if len(waypoints) > max_points:
            result = await self._route_chunked(
                waypoints=waypoints,
                transport_mode=transport_mode,
                constraints=constraints,
                transport=transport,
                max_points=max_points,
            )
        else:
            result = await self._route_once(
                waypoints=waypoints,
                transport_mode=transport_mode,
                constraints=constraints,
                transport=transport,
            )

        if len(_route_cache) >= _CACHE_MAX_ITEMS:
            _route_cache.pop(next(iter(_route_cache)))
        _route_cache[cache_key] = (now, result)
        return result

    async def _route_chunked(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None,
        transport: str,
        max_points: int,
    ) -> RoutingResult:
        """Build an ordered route in overlapping provider-sized windows.

        2GIS limits intermediate points (and therefore the total points in a
        request).  Overlapping the last point of one window with the first of
        the next keeps the route continuous while consuming only a few API
        calls for Travel+ routes.  The global total limit is checked after
        aggregation, not independently for each window.
        """

        aggregate_legs: list[RouteLegResult] = []
        geometries: list[str] = []
        warnings: list[str] = ["provider_points_chunked"]
        total_distance = 0
        total_duration = 0
        elevation_gain = 0
        elevation_loss = 0
        have_elevation = False
        min_altitude: int | None = None
        max_altitude: int | None = None
        max_angle: float | None = None
        road_types: list[str] = []
        start = 0
        while start < len(waypoints) - 1:
            end = min(start + max_points - 1, len(waypoints) - 1)
            chunk = await self._route_once(
                waypoints=waypoints[start : end + 1],
                transport_mode=transport_mode,
                # A global max_total is checked below; per-window max_leg is
                # still useful for the provider's two-point safeguard.
                constraints=RoutingConstraints(
                    max_leg_meters=constraints.max_leg_meters if constraints else None,
                    max_total_meters=None,
                ),
                transport=transport,
            )
            aggregate_legs.extend(
                RouteLegResult(
                    from_index=leg.from_index + start,
                    to_index=leg.to_index + start,
                    distance_meters=leg.distance_meters,
                    duration_seconds=leg.duration_seconds,
                    geometry_wkt=leg.geometry_wkt,
                    warnings=leg.warnings,
                )
                for leg in chunk.legs
            )
            if chunk.geometry_wkt:
                geometries.append(chunk.geometry_wkt)
            warnings.extend(chunk.warnings)
            total_distance += chunk.total_distance_meters
            total_duration += chunk.total_duration_seconds
            if chunk.elevation_gain_meters is not None:
                have_elevation = True
                elevation_gain += chunk.elevation_gain_meters
            if chunk.elevation_loss_meters is not None:
                have_elevation = True
                elevation_loss += chunk.elevation_loss_meters
            if chunk.min_altitude_meters is not None:
                min_altitude = (
                    chunk.min_altitude_meters
                    if min_altitude is None
                    else min(min_altitude, chunk.min_altitude_meters)
                )
            if chunk.max_altitude_meters is not None:
                max_altitude = (
                    chunk.max_altitude_meters
                    if max_altitude is None
                    else max(max_altitude, chunk.max_altitude_meters)
                )
            if chunk.max_road_angle_degrees is not None:
                max_angle = (
                    chunk.max_road_angle_degrees
                    if max_angle is None
                    else max(max_angle, chunk.max_road_angle_degrees)
                )
            for road_type in chunk.road_types:
                if road_type not in road_types:
                    road_types.append(road_type)
            if end == len(waypoints) - 1:
                break
            start = end

        if constraints and constraints.max_total_meters is not None:
            max_total = constraints.max_total_meters
            if total_distance > max_total:
                raise RoutingError(
                    "route_too_long",
                    f"Route length {total_distance}m exceeds max {max_total}m",
                )
        if self._max_route_meters is not None and total_distance > self._max_route_meters:
            raise RoutingError(
                "route_too_long",
                f"Route length {total_distance}m exceeds 2GIS limit {self._max_route_meters}m",
            )
        geometry = _merge_linestrings(geometries)
        return RoutingResult(
            provider="2gis",
            synthetic=False,
            legs=tuple(aggregate_legs),
            total_distance_meters=total_distance,
            total_duration_seconds=total_duration,
            warnings=tuple(dict.fromkeys(warnings)),
            geometry_wkt=geometry,
            elevation_gain_meters=elevation_gain if have_elevation else None,
            elevation_loss_meters=elevation_loss if have_elevation else None,
            min_altitude_meters=min_altitude,
            max_altitude_meters=max_altitude,
            max_road_angle_degrees=max_angle,
            road_types=tuple(road_types),
        )

    async def _route_once(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None,
        transport: str,
    ) -> RoutingResult:
        point_payload: list[dict[str, Any]] = []
        for index, point in enumerate(waypoints):
            # ``walking`` attaches an endpoint to a safe pedestrian path even
            # when a place pin is not exactly on a road. ``pref`` is the
            # documented type for an intermediate stop and avoids treating it
            # as a second destination pair.
            point_type = (
                "walking"
                if transport_mode == "walk" or index in {0, len(waypoints) - 1}
                else "pref"
            )
            point_payload.append(
                {
                    "type": point_type,
                    "lon": point.lng,
                    "lat": point.lat,
                    **({"start": True} if index == 0 else {}),
                }
            )
        filters = list(self._filters)
        if transport_mode == "walk":
            for safety_filter in ("highway", "ban_stairway"):
                if safety_filter not in filters:
                    filters.append(safety_filter)
        payload: dict[str, Any] = {
            "points": point_payload,
            "transport": transport,
            "route_mode": "fastest",
            "output": "detailed",
            "locale": "ru",
            "alternative": self._alternative,
            "filters": filters,
            "allow_locked_roads": False,
        }
        if transport_mode == "car":
            # 2GIS uses current jam data by default; make that choice explicit
            # so a future traffic-mode setting cannot silently change semantics.
            payload["traffic_mode"] = "jam"
        if transport_mode == "walk":
            payload["need_altitudes"] = True
            payload["params"] = {"pedestrian": {"use_instructions": True}}

        try:
            response = await self._post(payload)
            response.raise_for_status()
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise RoutingError(
                    "routing_provider_error",
                    "2GIS routing response is too large",
                )
            data = response.json()
        except httpx.TimeoutException as exc:
            raise RoutingError("routing_timeout", "2GIS routing timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise RoutingError(
                _http_error_code(exc.response),
                "2GIS routing request failed",
            ) from exc
        except httpx.HTTPError as exc:
            raise RoutingError("routing_provider_error", "2GIS routing is unavailable") from exc
        except ValueError as exc:
            raise RoutingError("routing_provider_error", "2GIS returned invalid JSON") from exc

        if not isinstance(data, Mapping):
            raise RoutingError("routing_provider_error", "2GIS returned an invalid response")
        status = str(data.get("status") or "OK").upper()
        response_type = str(data.get("type") or "result").lower()
        result_items = data.get("result")
        if status != "OK" or response_type == "error" or not isinstance(result_items, list):
            provider_message = data.get("message")
            # Never echo the request URL or query string (which contains key).
            detail = provider_message.strip() if isinstance(provider_message, str) else ""
            if detail and self._api_key:
                detail = detail.replace(self._api_key, "[redacted]")
            suffix = f": {detail[:180]}" if detail else ""
            code = (
                "routing_unreachable"
                if status
                in {
                    "POINT_EXCLUDED",
                    "ROUTE_NOT_FOUND",
                    "ROUTE_DOES_NOT_EXISTS",
                    "ATTRACT_FAIL",
                }
                else "routing_provider_error"
            )
            raise RoutingError(code, f"2GIS не построил маршрут{suffix}")
        if not result_items or not isinstance(result_items[0], Mapping):
            raise RoutingError("routing_unreachable", "2GIS не вернул вариант маршрута")

        result = result_items[0]
        total_distance = _as_non_negative_int(result.get("total_distance"), field="total_distance")
        total_duration = _as_non_negative_int(result.get("total_duration"), field="total_duration")
        limits = constraints or RoutingConstraints()
        max_total = limits.max_total_meters
        if max_total is not None and total_distance > max_total:
            raise RoutingError(
                "route_too_long",
                f"Route length {total_distance}m exceeds max {max_total}m",
            )
        if self._max_route_meters is not None and total_distance > self._max_route_meters:
            raise RoutingError(
                "route_too_long",
                f"Route length {total_distance}m exceeds 2GIS limit {self._max_route_meters}m",
            )
        max_leg = limits.max_leg_meters or default_max_leg_meters(transport_mode)
        warnings: list[str] = []
        if len(waypoints) > 2:
            warnings.append("provider_leg_geometry_aggregated")
        if total_distance > max_leg and len(waypoints) == 2:
            raise RoutingError(
                "routing_unreachable",
                f"Leg exceeds max distance ({total_distance}m > {max_leg}m)",
            )

        road_types_raw = result.get("filter_road_types")
        road_types = tuple(
            str(item).strip()
            for item in _as_items(road_types_raw)
            if isinstance(item, str) and item.strip()
        )
        if road_types:
            warnings.append("provider_returned_filtered_road_type")

        altitudes = result.get("altitudes_info")
        elevation_gain = elevation_loss = None
        min_altitude = max_altitude = None
        max_angle: float | None = None
        if isinstance(altitudes, Mapping):
            # 2GIS returns elevation changes in centimetres. The application
            # contract is metres, so convert at the provider boundary and
            # avoid presenting a 10,690 cm gain as 10,690 metres.
            raw_gain = _optional_non_negative_int(altitudes.get("elevation_gain"))
            raw_loss = _optional_non_negative_int(altitudes.get("elevation_loss"))
            elevation_gain = None if raw_gain is None else round(raw_gain / 100)
            elevation_loss = None if raw_loss is None else round(raw_loss / 100)
            raw_min_altitude = _optional_int(altitudes.get("min_altitude"))
            raw_max_altitude = _optional_int(altitudes.get("max_altitude"))
            min_altitude = None if raw_min_altitude is None else round(raw_min_altitude / 100)
            max_altitude = None if raw_max_altitude is None else round(raw_max_altitude / 100)
            raw_angle = altitudes.get("max_road_angle")
            if (
                isinstance(raw_angle, (int, float))
                and not isinstance(raw_angle, bool)
                and isfinite(float(raw_angle))
            ):
                parsed_angle = float(raw_angle)
                if 0 <= parsed_angle <= 90:
                    max_angle = parsed_angle

        geometry = _route_geometry(result)
        if geometry is None:
            warnings.append("provider_geometry_missing")

        leg = RouteLegResult(
            from_index=0,
            to_index=len(waypoints) - 1,
            distance_meters=total_distance,
            duration_seconds=total_duration,
            geometry_wkt=geometry,
            warnings=tuple(warnings),
        )
        return RoutingResult(
            provider="2gis",
            synthetic=False,
            legs=(leg,),
            total_distance_meters=total_distance,
            total_duration_seconds=total_duration,
            warnings=tuple(warnings),
            geometry_wkt=geometry,
            elevation_gain_meters=elevation_gain,
            elevation_loss_meters=elevation_loss,
            min_altitude_meters=min_altitude,
            max_altitude_meters=max_altitude,
            max_road_angle_degrees=max_angle,
            road_types=road_types,
        )

    async def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        if _circuit.is_open():
            _stats.circuit_open_rejections += 1
            raise RoutingError(
                "routing_circuit_open",
                "2GIS routing temporarily disabled after repeated failures",
            )

        _stats.calls_total += 1
        _stats.record_daily_call(self._daily_call_budget)

        last_error: httpx.HTTPError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            started = time.monotonic()
            try:
                response = await self._send(payload)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt + 1 < _RETRY_ATTEMPTS:
                    _stats.retries += 1
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                _circuit.record_failure()
                _stats.failures_total += 1
                _logger.warning(
                    "two_gis_routing_call",
                    extra={
                        "outcome": "timeout",
                        "attempts": attempt + 1,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                raise
            else:
                _circuit.record_success()
                if response.status_code == 429:
                    _stats.quota_errors_total += 1
                _logger.info(
                    "two_gis_routing_call",
                    extra={
                        "outcome": "ok" if response.status_code < 400 else "http_error",
                        "status_code": response.status_code,
                        "attempts": attempt + 1,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                return response
        # Unreachable: the loop above always returns or raises on its last attempt.
        if last_error is None:
            raise RuntimeError("2GIS retry loop exited without a result")
        raise last_error

    async def _send(self, payload: Mapping[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                f"{self._base_url}{_ROUTING_PATH}",
                params={"key": self._api_key},
                json=payload,
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(
                f"{self._base_url}{_ROUTING_PATH}",
                params={"key": self._api_key},
                json=payload,
            )


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        return None
    parsed = int(round(value))
    return parsed if parsed >= 0 else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(float(value)):
        return None
    return int(round(value))
