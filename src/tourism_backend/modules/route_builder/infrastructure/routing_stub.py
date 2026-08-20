"""Deterministic synthetic RoutingProvider (not navigation-grade)."""

from __future__ import annotations

import math

from tourism_backend.modules.route_builder.application.routing import (
    RouteLegResult,
    RouteWaypoint,
    RoutingConstraints,
    RoutingError,
    RoutingResult,
    TransportMode,
    default_max_leg_meters,
)

# Straight-line → rough road estimate. Explicitly synthetic.
_ROAD_FACTOR = 1.35
_SPEED_KMH: dict[TransportMode, float] = {
    "walk": 4.5,
    "car": 45.0,
    "public": 25.0,
    "mixed": 30.0,
}


def _haversine_m(a: RouteWaypoint, b: RouteWaypoint) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def _line_wkt(a: RouteWaypoint, b: RouteWaypoint) -> str:
    return f"LINESTRING({a.lng} {a.lat}, {b.lng} {b.lat})"


class StubRoutingProvider:
    """Haversine × road factor. Always sets synthetic=True."""

    async def route(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None = None,
    ) -> RoutingResult:
        if len(waypoints) < 2:
            raise RoutingError(
                code="routing_provider_error",
                message="At least two waypoints are required",
            )
        limits = constraints or RoutingConstraints()
        max_leg = limits.max_leg_meters or default_max_leg_meters(transport_mode)
        max_total = limits.max_total_meters
        speed = _SPEED_KMH[transport_mode]

        legs: list[RouteLegResult] = []
        total_distance = 0
        total_duration = 0
        warnings: list[str] = ["synthetic_straight_line", "stub_routing_not_navigation_grade"]

        for index in range(len(waypoints) - 1):
            start, end = waypoints[index], waypoints[index + 1]
            distance = int(round(_haversine_m(start, end) * _ROAD_FACTOR))
            if distance > max_leg:
                raise RoutingError(
                    code="routing_unreachable",
                    message=(
                        f"Leg {index}->{index + 1} exceeds max distance "
                        f"({distance}m > {max_leg}m) for mode {transport_mode}"
                    ),
                )
            duration = int(round((distance / 1000.0) / speed * 3600.0)) if speed > 0 else 0
            total_distance += distance
            total_duration += duration
            legs.append(
                RouteLegResult(
                    from_index=index,
                    to_index=index + 1,
                    distance_meters=distance,
                    duration_seconds=duration,
                    geometry_wkt=_line_wkt(start, end),
                    warnings=("synthetic_straight_line",),
                )
            )

        if max_total is not None and total_distance > max_total:
            raise RoutingError(
                code="route_too_long",
                message=f"Route length {total_distance}m exceeds max {max_total}m",
            )

        return RoutingResult(
            provider="stub",
            synthetic=True,
            legs=tuple(legs),
            total_distance_meters=total_distance,
            total_duration_seconds=total_duration,
            warnings=tuple(warnings),
        )
