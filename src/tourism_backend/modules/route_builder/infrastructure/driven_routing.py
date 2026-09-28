"""Valhalla with car parks: drives end where a walk to the stop begins (spec 14b).

Walking routes go straight to Valhalla as before, and leg by leg when one
pair has no path. Car and mixed routes are
built leg by leg by ``mixed_legs.build_driven_route`` and carry their
segments; a mixed route is driven until public transport is routed (12b).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from tourism_backend.modules.route_builder.application.mixed_legs import (
    build_driven_route,
    build_walked_route,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingConstraints,
    RoutingError,
    RoutingResult,
    TransportMode,
)
from tourism_backend.modules.route_builder.infrastructure.parkings import parking_index
from tourism_backend.modules.route_builder.infrastructure.valhalla_routing import (
    ValhallaRoutingProvider,
)

_DRIVEN: frozenset[str] = frozenset({"car", "mixed"})


class DrivenRoutingProvider:
    def __init__(self, valhalla: ValhallaRoutingProvider, *, settings: Any) -> None:
        self._valhalla = valhalla
        self._settings = settings

    async def _parkings(self, stop: RouteWaypoint) -> Sequence[tuple[float, float]]:
        index = await parking_index(self._settings)
        return [(p.lng, p.lat) for p in index.nearest(stop.lng, stop.lat)]

    async def route(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None = None,
    ) -> RoutingResult:
        if transport_mode not in _DRIVEN:
            try:
                return await self._valhalla.route(
                    waypoints=waypoints, transport_mode=transport_mode, constraints=constraints
                )
            except RoutingError as exc:
                # A caller's own limit stands; a pair with no path does not
                # flatten the other legs (spec 14, D24).
                if exc.code != "routing_unreachable" or constraints is not None:
                    raise
                if len(waypoints) < 3:
                    raise
            built = await build_walked_route(waypoints, router=self._valhalla)
        else:
            built = await build_driven_route(
                waypoints, router=self._valhalla, parkings=self._parkings
            )
        if built.result.synthetic:
            # Nothing was routed at all: let callers fall back as before.
            raise RoutingError("routing_unreachable", "Valhalla не построил ни одного участка")
        return replace(
            built.result,
            segments=tuple(segment.as_meta() for segment in built.segments),
            warnings=tuple(built.warnings),
        )
