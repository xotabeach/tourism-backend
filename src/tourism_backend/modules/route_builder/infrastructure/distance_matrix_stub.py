"""No-op distance matrix provider: no data, never fails. Local/test default."""

from __future__ import annotations

from tourism_backend.modules.route_builder.application.distance_matrix import (
    DistanceMatrixResult,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)


class StubDistanceMatrixProvider:
    async def compute(
        self,
        *,
        sources: list[RouteWaypoint],
        targets: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> DistanceMatrixResult:
        _ = sources, targets, transport_mode
        return DistanceMatrixResult(entries=())
