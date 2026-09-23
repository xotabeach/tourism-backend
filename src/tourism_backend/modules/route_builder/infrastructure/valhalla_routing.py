"""Valhalla routing adapter over our own OSM graph (spec 12a, section 2).

Valhalla runs inside the private compose network with a Crimea graph built
from OpenStreetMap (tourism-platform/osm-build). Nothing here talks to a
third party, so answers may be stored: they are ODbL data we attribute.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from math import atan, degrees, isfinite
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

_logger = logging.getLogger("tourism_backend.valhalla_routing")

_COSTING: dict[TransportMode, str] = {"walk": "pedestrian", "car": "auto"}
# Crimean trails are mostly SAC T2-T3; Valhalla's default of 1 keeps walkers
# on roads. Tuned on the comparison run (spec 12a, section 4).
_PEDESTRIAN_OPTIONS: dict[str, Any] = {"max_hiking_difficulty": 4, "use_ferry": 0}
# Coastal boats (Yalta - Alupka) are OSM ferries; a walk or a drive must not
# cross the sea, as 2GIS's "ferry" filter ensured before.
_AUTO_OPTIONS: dict[str, Any] = {"use_ferry": 0}
# Sample spacing of the elevation profile and the smoothing window over it:
# SRTM is noisy at ~30 m, a raw sum would inflate the climb.
_ELEVATION_INTERVAL_M = 30
_SMOOTHING_WINDOW = 5
_SLOPE_SPAN_M = 50
# Snap only to roads connected to at least this many others: a palace or a
# park point otherwise lands on an isolated service road inside a fence and
# no car path exists (Livadia Palace did exactly that).
_MIN_REACHABILITY = 200
# Valhalla error codes meaning "no way between these points".
_UNREACHABLE_CODES = frozenset({170, 171, 442, 443})
_MAX_RESPONSE_BYTES = 8_000_000


def decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Google polyline with 1e6 precision, as (lng, lat) pairs."""
    points: list[tuple[float, float]] = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= length:
                    raise RoutingError("routing_provider_error", "Valhalla shape is truncated")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        points.append((lng / 1e6, lat / 1e6))
    return points


def _wkt(points: Sequence[tuple[float, float]]) -> str | None:
    if len(points) < 2:
        return None
    return "LINESTRING(" + ", ".join(f"{lng:.6f} {lat:.6f}" for lng, lat in points) + ")"


def _smoothed(values: Sequence[float], window: int) -> list[float]:
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - half) : i + half + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _profile_stats(
    heights: Sequence[float],
) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    """Gain, loss, min, max and the steepest 50 m stretch of a profile."""
    samples = [h for h in heights if isinstance(h, (int, float)) and isfinite(h)]
    if len(samples) < 2:
        return None, None, None, None, None
    smooth = _smoothed(samples, _SMOOTHING_WINDOW)
    gain = loss = 0.0
    for before, after in zip(smooth, smooth[1:], strict=False):
        step = after - before
        if step > 0:
            gain += step
        else:
            loss -= step
    span = max(1, round(_SLOPE_SPAN_M / _ELEVATION_INTERVAL_M))
    steepest = 0.0
    for i in range(len(smooth) - span):
        rise = abs(smooth[i + span] - smooth[i])
        steepest = max(steepest, degrees(atan(rise / (span * _ELEVATION_INTERVAL_M))))
    return round(gain), round(loss), round(min(samples)), round(max(samples)), steepest


class ValhallaRoutingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
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
                raise RoutingError("routing_request_invalid", "Waypoint coordinates are invalid")
        costing = _COSTING.get(transport_mode)
        if costing is None:
            raise RoutingError(
                "routing_unsupported_mode",
                f"Valhalla routing does not support mode {transport_mode!r} yet",
            )
        payload: dict[str, Any] = {
            "locations": [
                {
                    "lat": point.lat,
                    "lon": point.lng,
                    "type": "break",
                    "minimum_reachability": _MIN_REACHABILITY,
                }
                for point in waypoints
            ],
            "costing": costing,
            "directions_type": "none",
            "shape_format": "polyline6",
            "elevation_interval": _ELEVATION_INTERVAL_M,
            "units": "kilometers",
        }
        payload["costing_options"] = {
            costing: _PEDESTRIAN_OPTIONS if costing == "pedestrian" else _AUTO_OPTIONS
        }
        data = await self._post(payload)
        return self._parse(data, waypoints, transport_mode, constraints or RoutingConstraints())

    async def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = f"{self._base_url}/route"
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise RoutingError("routing_timeout", "Valhalla routing timed out") from exc
        except httpx.HTTPError as exc:
            raise RoutingError("routing_provider_error", "Valhalla is unavailable") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise RoutingError("routing_provider_error", "Valhalla response is too large")
        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingError("routing_provider_error", "Valhalla returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise RoutingError("routing_provider_error", "Valhalla returned an invalid response")
        if response.status_code >= 400:
            code = data.get("error_code")
            message = str(data.get("error") or "")[:180]
            if isinstance(code, int) and code in _UNREACHABLE_CODES:
                raise RoutingError(
                    "routing_unreachable", f"Valhalla не построил маршрут: {message}"
                )
            _logger.warning(
                "valhalla_route_failed",
                extra={"status": response.status_code, "error_code": code},
            )
            raise RoutingError("routing_provider_error", f"Valhalla error: {message}")
        return data

    def _parse(
        self,
        data: Mapping[str, Any],
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        limits: RoutingConstraints,
    ) -> RoutingResult:
        trip = data.get("trip")
        raw_legs = trip.get("legs") if isinstance(trip, Mapping) else None
        if not isinstance(raw_legs, list) or len(raw_legs) != len(waypoints) - 1:
            raise RoutingError("routing_provider_error", "Valhalla returned no legs")
        max_leg = limits.max_leg_meters or default_max_leg_meters(transport_mode)
        legs: list[RouteLegResult] = []
        line: list[tuple[float, float]] = []
        heights: list[float] = []
        for index, raw in enumerate(raw_legs):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("summary"), Mapping):
                raise RoutingError("routing_provider_error", "Valhalla leg is malformed")
            summary = raw["summary"]
            meters = round(float(summary.get("length") or 0) * 1000)
            seconds = round(float(summary.get("time") or 0))
            if meters > max_leg:
                raise RoutingError(
                    "routing_unreachable",
                    f"Leg exceeds max distance ({meters}m > {max_leg}m)",
                )
            shape = raw.get("shape")
            points = decode_polyline6(shape) if isinstance(shape, str) else []
            # Legs share their joint point; keep it once in the whole line.
            line.extend(points[1:] if line and points and points[0] == line[-1] else points)
            elevation = raw.get("elevation")
            if isinstance(elevation, list):
                heights.extend(float(h) for h in elevation if isinstance(h, (int, float)))
            legs.append(
                RouteLegResult(
                    from_index=index,
                    to_index=index + 1,
                    distance_meters=meters,
                    duration_seconds=seconds,
                    geometry_wkt=_wkt(points),
                )
            )
        total_distance = sum(leg.distance_meters for leg in legs)
        total_duration = sum(leg.duration_seconds for leg in legs)
        if limits.max_total_meters is not None and total_distance > limits.max_total_meters:
            raise RoutingError(
                "route_too_long",
                f"Route length {total_distance}m exceeds max {limits.max_total_meters}m",
            )
        warnings: list[str] = []
        geometry = _wkt(line)
        if geometry is None:
            warnings.append("provider_geometry_missing")
        gain, loss, low, high, steepest = _profile_stats(heights)
        return RoutingResult(
            provider="valhalla",
            synthetic=False,
            legs=tuple(legs),
            total_distance_meters=total_distance,
            total_duration_seconds=total_duration,
            warnings=tuple(warnings),
            geometry_wkt=geometry,
            elevation_gain_meters=gain,
            elevation_loss_meters=loss,
            min_altitude_meters=low,
            max_altitude_meters=high,
            max_road_angle_degrees=steepest,
        )
