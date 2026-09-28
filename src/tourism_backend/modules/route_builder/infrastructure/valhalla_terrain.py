"""Ground under a segment from our Valhalla graph (spec 17, section 3).

``trace_attributes`` walks the segment's own line over the graph and tells,
edge by edge, the OSM trail grade (``sac_scale``), surface, use and how
winding the road is. We keep only metres per category; the difficulty rules
turn them into levels.

The line is sent in pieces: ``trace_attributes`` has much tighter limits
than ``/route`` (about 200 km and 16 000 points), and a drive across Crimea
is longer than that (D21).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

_logger = logging.getLogger("tourism_backend.valhalla_terrain")

# Bump when categories or thresholds below change, so stored ground is
# fetched again.
TERRAIN_VERSION = 1

_COSTING = {"walk": "pedestrian", "car": "auto"}
_OPTIONS: dict[str, dict[str, Any]] = {
    # Every grade, so the trace follows the route's own trail.
    "pedestrian": {"pedestrian": {"max_hiking_difficulty": 6}},
    "auto": {},
}
_ATTRIBUTES = [
    "edge.length",
    "edge.sac_scale",
    "edge.surface",
    "edge.use",
    "edge.unpaved",
    "edge.curvature",
]
# Valhalla's curvature is 0..15; hairpins of a mountain road are 13 and up
# (Yalta - Ai-Petri: 15 km of 27; the Simferopol - Alushta highway: 2 of 49).
SERPENTINE_CURVATURE = 13
# Surfaces a car should not be on without clearance or 4x4.
_OFFROAD_SURFACES = frozenset({"path", "impassable"})
_TRAIL_USES = frozenset({"path", "track", "footway", "bridleway"})
_MAX_PIECE_POINTS = 4_000
_MAX_PIECE_METERS = 100_000


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def pieces(line: Sequence[Sequence[float]]) -> list[list[Sequence[float]]]:
    """Split a [lng, lat] line into overlapping pieces within the limits."""
    if len(line) < 2:
        return []
    result: list[list[Sequence[float]]] = []
    current: list[Sequence[float]] = [line[0]]
    meters = 0.0
    for point in line[1:]:
        meters += _distance(current[-1], point)
        current.append(point)
        if len(current) >= _MAX_PIECE_POINTS or meters >= _MAX_PIECE_METERS:
            result.append(current)
            current, meters = [point], 0.0
    if len(current) >= 2:
        result.append(current)
    return result


def ground_meters(edges: Sequence[Mapping[str, Any]], *, mode: str) -> dict[str, int]:
    """Metres per ground category from ``trace_attributes`` edges."""
    totals: dict[str, float] = {}

    def add(key: str, km: float) -> None:
        totals[key] = totals.get(key, 0.0) + km * 1000

    for edge in edges:
        length = edge.get("length")
        if not isinstance(length, (int, float)) or length <= 0:
            continue
        if mode == "walk":
            grade = edge.get("sac_scale")
            if isinstance(grade, int) and 1 <= grade <= 6:
                add(f"T{grade}", length)
            elif edge.get("unpaved") is True and edge.get("use") in _TRAIL_USES:
                # A path or forest road without a grade in OSM: most Crimean
                # trails are tagged like this (Chufut-Kale, 2026-09-25).
                add("dirt", length)
            if edge.get("use") == "steps":
                add("steps", length)
        else:
            surface = edge.get("surface")
            if surface in _OFFROAD_SURFACES:
                add("offroad", length)
            elif edge.get("unpaved") is True:
                add("unpaved", length)
            curvature = edge.get("curvature")
            if isinstance(curvature, int) and curvature >= SERPENTINE_CURVATURE:
                add("serpentine", length)
    return {key: round(value) for key, value in totals.items() if value >= 1}


class ValhallaTerrainClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 20.0) -> None:
        self._url = f"{base_url.rstrip('/')}/trace_attributes"
        self._timeout = timeout_seconds

    async def segment_ground(
        self, line: Sequence[Sequence[float]], *, mode: str
    ) -> dict[str, int] | None:
        """Metres per category along a [lng, lat] line; None if Valhalla
        could not match it (the caller keeps the estimate «примерно»)."""
        costing = _COSTING.get(mode)
        if costing is None:
            return {}
        totals: dict[str, int] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for piece in pieces(line):
                edges = await self._edges(client, piece, costing)
                if edges is None:
                    return None
                for key, meters in ground_meters(edges, mode=mode).items():
                    totals[key] = totals.get(key, 0) + meters
        return totals

    async def _edges(
        self, client: httpx.AsyncClient, piece: Sequence[Sequence[float]], costing: str
    ) -> list[Mapping[str, Any]] | None:
        shape = [{"lat": point[1], "lon": point[0]} for point in piece]
        # The line came out of this very graph, so walking its edges is
        # exact; a line drawn by an editor needs the map matcher.
        for match in ("edge_walk", "map_snap"):
            payload = {
                "shape": shape,
                "costing": costing,
                "costing_options": _OPTIONS[costing],
                "shape_match": match,
                "filters": {"attributes": _ATTRIBUTES, "action": "include"},
            }
            try:
                response = await client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                _logger.warning("valhalla_terrain_unavailable", extra={"error": str(exc)})
                return None
            if response.status_code < 400:
                data = response.json()
                edges = data.get("edges") if isinstance(data, Mapping) else None
                return [edge for edge in edges or [] if isinstance(edge, Mapping)]
        _logger.info("valhalla_terrain_unmatched", extra={"status": response.status_code})
        return None
