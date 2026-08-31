"""No-op TSP provider: keeps the caller-supplied order. Local/test default."""

from __future__ import annotations

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)
from tourism_backend.modules.route_builder.application.tsp import TspOrderResult


class StubTspProvider:
    async def optimize_order(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> TspOrderResult:
        _ = transport_mode
        return TspOrderResult(order=tuple(range(len(waypoints))), optimized=False)
