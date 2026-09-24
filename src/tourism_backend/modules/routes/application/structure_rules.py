"""Days and segments of a route, as pure rules (spec 14, step 0).

A route is split into days, and every leg between two stops into one or
more segments, each with its own way of travel. Step 0 only lays the model
down: every leg is one ``main`` segment in the route's own mode, and the
whole route is one day. Mixing car and walking (14b) and splitting into days
(14a) build on the same shapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

BaseMode = Literal["walk", "car", "mixed"]
SegmentMode = Literal["walk", "car", "bus", "trolleybus", "train", "cable_car", "ferry"]
SegmentRole = Literal["main", "approach", "return"]
SegmentOrigin = Literal["router", "synthetic", "editor", "transit"]

BASE_MODES: tuple[BaseMode, ...] = ("walk", "car", "mixed")
SEGMENT_MODES: tuple[SegmentMode, ...] = (
    "walk",
    "car",
    "bus",
    "trolleybus",
    "train",
    "cable_car",
    "ferry",
)

# Every spelling the routes table and older clients have used (spec 14,
# «Словарь способов»). Bicycles were never routed: such routes are walked.
_LEGACY_BASE_MODES: dict[str, BaseMode] = {
    "walk": "walk",
    "walking": "walk",
    "pedestrian": "walk",
    "foot": "walk",
    "hiking": "walk",
    "bicycle": "walk",
    "bike": "walk",
    "cycling": "walk",
    "car": "car",
    "driving": "car",
    "drive": "car",
    "auto": "car",
    "mixed": "mixed",
    "public": "mixed",
    "public_transport": "mixed",
    "transit": "mixed",
    "bus": "mixed",
}
_PUBLIC_SPELLINGS = frozenset({"public", "public_transport", "transit", "bus"})


def known_transport_mode(value: str | None) -> bool:
    return value is None or value.casefold().strip() in _LEGACY_BASE_MODES


def base_mode_for(transport_mode: str | None) -> BaseMode:
    """The route's base mode from any stored or client spelling; walk if unknown."""

    return _LEGACY_BASE_MODES.get((transport_mode or "").casefold().strip(), "walk")


def implies_public_transport(transport_mode: str | None) -> bool:
    return (transport_mode or "").casefold().strip() in _PUBLIC_SPELLINGS


def segment_mode_for(base_mode: str | None) -> Literal["walk", "car"]:
    """Mode of an automatically built segment.

    Until mixing lands (14b) a mixed route is driven: buses are not routed
    before 12b, and walking a car route's legs would invent kilometres.
    """

    return "walk" if base_mode_for(base_mode) == "walk" else "car"


@dataclass(frozen=True, slots=True)
class PlannedSegment:
    leg_index: int
    seq: int
    from_stop_id: UUID
    to_stop_id: UUID
    mode: SegmentMode
    role: SegmentRole
    origin: SegmentOrigin
    distance_meters: int | None
    duration_seconds: int | None
    elevation_gain_meters: int | None = None
    elevation_loss_meters: int | None = None


@dataclass(frozen=True, slots=True)
class PlannedDay:
    day_index: int
    first_stop_id: UUID
    last_stop_id: UUID
    boundary_source: Literal["auto", "manual"] = "auto"
    overloaded: bool = False
    overnight_note: str | None = None


def _leg_numbers(item: object) -> tuple[int, int] | None:
    if not isinstance(item, Mapping):
        return None
    distance = item.get("distance_meters")
    duration = item.get("duration_seconds")
    for value in (distance, duration):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
    return round(distance), round(duration)  # type: ignore[arg-type]


_ROLES = frozenset({"main", "approach", "return"})
_ORIGINS = frozenset({"router", "synthetic", "editor", "transit"})


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value)


def _routed_segments(stop_ids: Sequence[UUID], raw: object) -> list[PlannedSegment] | None:
    """Segments the router built (14b), if they cover every leg of these stops."""
    if not isinstance(raw, list) or not raw:
        return None
    segments: list[PlannedSegment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        leg, seq = item.get("leg_index"), item.get("seq")
        mode, role, origin = item.get("mode"), item.get("role"), item.get("origin")
        if (
            not isinstance(leg, int)
            or not isinstance(seq, int)
            or not 0 <= leg < len(stop_ids) - 1
            or seq < 0
            or mode not in SEGMENT_MODES
            or role not in _ROLES
            or origin not in _ORIGINS
        ):
            return None
        segments.append(
            PlannedSegment(
                leg_index=leg,
                seq=seq,
                from_stop_id=stop_ids[leg],
                to_stop_id=stop_ids[leg + 1],
                mode=mode,
                role=role,
                origin=origin,
                distance_meters=_optional_int(item.get("distance_meters")),
                duration_seconds=_optional_int(item.get("duration_seconds")),
                elevation_gain_meters=_optional_int(item.get("elevation_gain_meters")),
                elevation_loss_meters=_optional_int(item.get("elevation_loss_meters")),
            )
        )
    segments.sort(key=lambda s: (s.leg_index, s.seq))
    if {s.leg_index for s in segments} != set(range(len(stop_ids) - 1)):
        return None
    keys = [(s.leg_index, s.seq) for s in segments]
    return segments if len(keys) == len(set(keys)) else None


def segment_shapes(routing: Mapping[str, object] | None) -> dict[tuple[int, int], str]:
    """Encoded line of each routed segment, by (leg_index, seq)."""
    shapes: dict[tuple[int, int], str] = {}
    raw = (routing or {}).get("segments")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("shape"), str):
            leg, seq = item.get("leg_index"), item.get("seq")
            if isinstance(leg, int) and isinstance(seq, int):
                shapes[(leg, seq)] = str(item["shape"])
    return shapes


def plan_segments(
    stop_ids: Sequence[UUID],
    *,
    routing: Mapping[str, object] | None,
    base_mode: str | None,
) -> list[PlannedSegment]:
    """The router's segments when it built them, else one per leg in the route's mode.

    Distances come from the router's per-pair legs when the route kept them;
    otherwise the segment is ``synthetic`` and has no numbers, as the route
    itself only has a straight line there.
    """

    routed = _routed_segments(stop_ids, (routing or {}).get("segments"))
    if routed is not None:
        return routed
    raw_legs = (routing or {}).get("legs")
    legs: list[tuple[int, int]] | None = None
    if isinstance(raw_legs, list) and len(raw_legs) == max(0, len(stop_ids) - 1):
        parsed = [_leg_numbers(item) for item in raw_legs]
        if all(item is not None for item in parsed):
            legs = [item for item in parsed if item is not None]
    mode = segment_mode_for(base_mode)
    segments: list[PlannedSegment] = []
    for index in range(len(stop_ids) - 1):
        numbers = legs[index] if legs is not None else None
        segments.append(
            PlannedSegment(
                leg_index=index,
                seq=0,
                from_stop_id=stop_ids[index],
                to_stop_id=stop_ids[index + 1],
                mode=mode,
                role="main",
                origin="router" if numbers is not None else "synthetic",
                distance_meters=numbers[0] if numbers is not None else None,
                duration_seconds=numbers[1] if numbers is not None else None,
            )
        )
    return segments


def plan_single_day(stop_ids: Sequence[UUID]) -> list[PlannedDay]:
    if not stop_ids:
        return []
    return [PlannedDay(day_index=1, first_stop_id=stop_ids[0], last_stop_id=stop_ids[-1])]


def structure_signature(
    segments: Sequence[PlannedSegment],
    days: Sequence[PlannedDay],
) -> list[object]:
    """Plain, ordered data for the snapshot fingerprint."""

    return [
        [
            [
                s.leg_index,
                s.seq,
                str(s.from_stop_id),
                str(s.to_stop_id),
                s.mode,
                s.role,
                s.origin,
                s.distance_meters,
                s.duration_seconds,
                s.elevation_gain_meters,
                s.elevation_loss_meters,
            ]
            for s in segments
        ],
        [[d.day_index, str(d.first_stop_id), str(d.last_stop_id), d.boundary_source] for d in days],
    ]
