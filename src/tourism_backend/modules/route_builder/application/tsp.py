"""Stop-order optimization provider port (Workstream A).

Separate from ``routing.py``'s ``RoutingProvider``: this answers "in what
order should an already-selected set of stops be visited", not "what is the
road geometry between two points". A provider may fail or time out — callers
must treat that as "keep the caller's order", never as a hard error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)


@dataclass(frozen=True, slots=True)
class TspOrderResult:
    # Permutation of input indices, e.g. (0, 2, 1, 3) for a 4-point input.
    # Always starts at 0 — the anchor/start point is never reordered away.
    order: tuple[int, ...]
    optimized: bool
    warnings: tuple[str, ...] = ()


class TspError(Exception):
    """Typed stop-order optimization failure. Always non-fatal to the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TspProvider(Protocol):
    async def optimize_order(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> TspOrderResult:
        """Return a visiting order for ``waypoints``, anchored at index 0."""
        ...
