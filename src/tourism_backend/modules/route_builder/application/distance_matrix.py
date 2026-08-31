"""Batched point-to-point distance/duration provider port (Workstream A).

Answers "how far/long from these sources to these targets", in one call —
distinct from ``RoutingProvider`` (one full road path) and ``TspProvider``
(best visiting order for one point set). A provider may fail; callers must
treat a failure as "no distance data available", never as a hard error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    TransportMode,
)


@dataclass(frozen=True, slots=True)
class DistanceMatrixEntry:
    source_index: int
    target_index: int
    # None when the provider could not find a path for this specific pair
    # (e.g. an island target for a driving matrix) — a missing number, not a
    # fabricated one.
    distance_meters: int | None
    duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class DistanceMatrixResult:
    entries: tuple[DistanceMatrixEntry, ...]

    def duration_seconds(self, *, source_index: int, target_index: int) -> int | None:
        for entry in self.entries:
            if entry.source_index == source_index and entry.target_index == target_index:
                return entry.duration_seconds
        return None

    def distance_meters(self, *, source_index: int, target_index: int) -> int | None:
        for entry in self.entries:
            if entry.source_index == source_index and entry.target_index == target_index:
                return entry.distance_meters
        return None


class DistanceMatrixError(Exception):
    """Typed distance-matrix failure. Always non-fatal to the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DistanceMatrixProvider(Protocol):
    async def compute(
        self,
        *,
        sources: list[RouteWaypoint],
        targets: list[RouteWaypoint],
        transport_mode: TransportMode,
    ) -> DistanceMatrixResult:
        """Return distance/duration for every (source, target) pair."""
        ...
