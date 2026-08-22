"""RoutingProvider application port (ADR-004)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

TransportMode = Literal["walk", "car", "public", "mixed"]


@dataclass(frozen=True, slots=True)
class RouteWaypoint:
    lng: float
    lat: float
    place_id: UUID | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingConstraints:
    max_leg_meters: int | None = None
    max_total_meters: int | None = None


@dataclass(frozen=True, slots=True)
class RouteLegResult:
    from_index: int
    to_index: int
    distance_meters: int
    duration_seconds: int
    geometry_wkt: str | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingResult:
    provider: str
    synthetic: bool
    legs: tuple[RouteLegResult, ...]
    total_distance_meters: int
    total_duration_seconds: int
    warnings: tuple[str, ...] = ()


class RoutingError(Exception):
    """Typed routing failure mapped to AppError by the application layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RoutingProvider(Protocol):
    async def route(
        self,
        *,
        waypoints: list[RouteWaypoint],
        transport_mode: TransportMode,
        constraints: RoutingConstraints | None = None,
    ) -> RoutingResult:
        """Compute road legs between ordered waypoints."""
        ...


def default_max_leg_meters(mode: TransportMode) -> int:
    return {
        "walk": 25_000,
        "car": 120_000,
        "public": 80_000,
        "mixed": 100_000,
    }[mode]


def normalize_transport_mode(mode: str | None) -> TransportMode:
    if mode in {"walk", "car", "public", "mixed"}:
        return mode  # type: ignore[return-value]
    return "walk"
