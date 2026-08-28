"""Create and expose append-only routing snapshots for route execution."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from geoalchemy2 import WKTElement
from geoalchemy2.functions import ST_AsGeoJSON, ST_AsText
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionRoutingOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteRoutingSnapshot,
)
from tourism_backend.modules.routes.application.schemas import RouteGeometryOut, RouteQualityStatus
from tourism_backend.modules.routes.infrastructure.models import Route

_QUALITY_STATUSES = {
    "unknown",
    "unverified",
    "checking",
    "verified",
    "verified_with_warnings",
    "needs_review",
    "unusable",
}
_MAX_WARNING_ITEMS = 32
_MAX_ROAD_TYPE_ITEMS = 32
_MAX_STRING_LENGTH = 160
_MAX_GEOMETRY_LENGTH = 2_000_000
_MAX_GEOMETRY_POINTS = 50_000


def _bounded_string(value: object, *, max_length: int = _MAX_STRING_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:max_length] if cleaned else None


def _bounded_strings(value: object, *, max_items: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        cleaned = _bounded_string(item)
        if cleaned is not None and cleaned not in result:
            result.append(cleaned)
        if len(result) >= max_items:
            break
    return result


def _non_negative_int(value: object, *, maximum: int = 2_147_483_647) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    integer = int(value)
    if integer < 0 or integer > maximum:
        return None
    return integer


def _altitude_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    integer = int(value)
    return integer if -500 <= integer <= 9_000 else None


def _angle(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    candidate = float(value)
    return candidate if math.isfinite(candidate) and 0 <= candidate <= 90 else None


def _json_safe(value: object, *, depth: int = 0) -> object:
    """Keep audit metadata bounded and JSON-only; never copy provider payloads."""

    if depth > 2:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:32]:
            if isinstance(key, str):
                result[key[:64]] = _json_safe(item, depth=depth + 1)
        return result
    return None


def _routing_metadata(route: Route) -> dict[str, Any]:
    value = route.accessibility
    if not isinstance(value, dict):
        return {}
    routing = value.get("routing")
    return routing if isinstance(routing, dict) else {}


def routing_snapshot_fingerprint(
    route: Route,
    *,
    geometry_wkt: str | None,
    stop_signature: Sequence[tuple[UUID, int, UUID]],
) -> str:
    """Return a stable digest of all routing-relevant route state.

    Names and descriptions are intentionally excluded: editing copy should not
    create a new route graph revision. Stops, geometry and normalized routing
    metadata are included, so an execution never silently follows a changed
    path.
    """

    routing = _routing_metadata(route)
    accessibility = route.accessibility if isinstance(route.accessibility, dict) else {}
    relevant = {
        "route_id": str(route.id),
        "transport_mode": route.transport_mode,
        "distance_meters": route.distance_meters,
        "estimated_duration_minutes": route.estimated_duration_minutes,
        "difficulty": route.difficulty,
        "suitable_for_children": route.suitable_for_children,
        "pets_allowed": route.pets_allowed,
        "seasonality": route.seasonality or [],
        "accessibility": {
            key: _json_safe(accessibility.get(key))
            for key in (
                "travel_pace",
                "day_kind",
                "budget_amount",
                "filters",
                "interests",
                "season",
                "with_children",
                "with_pets",
            )
            if key in accessibility
        },
        "routing": _json_safe(routing),
        "geometry_wkt": geometry_wkt if isinstance(geometry_wkt, str) else None,
        "stops": [
            [str(place_id), int(position), str(route_stop_id)]
            for route_stop_id, position, place_id in stop_signature
        ],
    }
    canonical = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _line_geometry(value: str | None) -> WKTElement | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if len(cleaned) > _MAX_GEOMETRY_LENGTH:
        return None
    open_index = cleaned.find("(")
    prefix = cleaned[:open_index].strip().upper() if open_index >= 0 else ""
    if prefix not in {"LINESTRING", "LINESTRING Z"} or not cleaned.endswith(")"):
        return None
    raw_points = cleaned[open_index + 1 : -1].split(",")
    if not 2 <= len(raw_points) <= _MAX_GEOMETRY_POINTS:
        return None
    normalized: list[str] = []
    for raw_point in raw_points:
        values = raw_point.split()
        if len(values) < 2:
            return None
        try:
            longitude = float(values[0])
            latitude = float(values[1])
        except ValueError:
            return None
        if (
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            return None
        normalized.append(f"{values[0]} {values[1]}")
    return WKTElement(f"LINESTRING({', '.join(normalized)})", srid=4326)


async def ensure_routing_snapshot(
    session: AsyncSession,
    *,
    route: Route,
    stop_signature: Sequence[tuple[UUID, int, UUID]],
    captured_at: datetime | None = None,
) -> RouteRoutingSnapshot:
    """Reuse the current revision or append a new immutable snapshot.

    The caller locks the route row before invoking this function. That makes
    the ``latest revision + 1`` operation safe for simultaneous users starting
    the same route while retaining a simple unique constraint in PostgreSQL.
    """

    captured = captured_at or datetime.now(UTC)
    geometry_raw = await session.scalar(
        select(ST_AsText(Route.geometry)).where(Route.id == route.id)
    )
    geometry_wkt = geometry_raw if isinstance(geometry_raw, str) else None
    fingerprint = routing_snapshot_fingerprint(
        route,
        geometry_wkt=geometry_wkt,
        stop_signature=stop_signature,
    )
    latest_stmt: Select[tuple[RouteRoutingSnapshot]] = (
        select(RouteRoutingSnapshot)
        .where(RouteRoutingSnapshot.route_id == route.id)
        .order_by(RouteRoutingSnapshot.revision.desc())
        .limit(1)
        .with_for_update()
    )
    latest = await session.scalar(latest_stmt)
    if latest is not None and latest.fingerprint == fingerprint:
        return latest

    routing = _routing_metadata(route)
    raw_quality = routing.get("quality_status")
    quality_status = raw_quality if isinstance(raw_quality, str) else "unknown"
    if quality_status not in _QUALITY_STATUSES:
        quality_status = "unknown"
    requested: dict[str, object] = {}
    if isinstance(route.accessibility, dict):
        for key in (
            "travel_pace",
            "day_kind",
            "budget_amount",
            "filters",
            "interests",
            "season",
            "with_children",
            "with_pets",
        ):
            if key in route.accessibility:
                requested[key] = _json_safe(route.accessibility[key])

    snapshot = RouteRoutingSnapshot(
        id=uuid4(),
        route_id=route.id,
        revision=(latest.revision + 1) if latest is not None else 1,
        fingerprint=fingerprint,
        provider=_bounded_string(routing.get("provider"), max_length=32),
        provider_version=_bounded_string(routing.get("provider_version"), max_length=64),
        transport_mode=_bounded_string(
            routing.get("transport_mode") or route.transport_mode,
            max_length=32,
        ),
        geometry=_line_geometry(geometry_wkt),
        distance_meters=_non_negative_int(routing.get("distance_meters", route.distance_meters)),
        movement_duration_seconds=_non_negative_int(routing.get("movement_duration_seconds")),
        visit_duration_minutes=_non_negative_int(routing.get("visit_duration_minutes")),
        transfer_duration_seconds=_non_negative_int(routing.get("transfer_duration_seconds")),
        buffer_duration_seconds=_non_negative_int(routing.get("buffer_duration_seconds")),
        total_duration_seconds=_non_negative_int(routing.get("total_duration_seconds")),
        elevation_gain_meters=_non_negative_int(routing.get("elevation_gain_meters")),
        elevation_loss_meters=_non_negative_int(routing.get("elevation_loss_meters")),
        min_altitude_meters=_altitude_int(routing.get("min_altitude_meters")),
        max_altitude_meters=_altitude_int(routing.get("max_altitude_meters")),
        max_road_angle_degrees=_angle(routing.get("max_road_angle_degrees")),
        road_types=_bounded_strings(
            routing.get("road_types"),
            max_items=_MAX_ROAD_TYPE_ITEMS,
        ),
        quality_status=quality_status,
        quality_policy_version=_bounded_string(
            routing.get("quality_policy_version"),
            max_length=32,
        ),
        warnings=_bounded_strings(
            routing.get("warnings"),
            max_items=_MAX_WARNING_ITEMS,
        ),
        requested_filters=requested or None,
        route_updated_at=route.updated_at,
        captured_at=captured,
        created_at=captured,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


def _geometry_from_geojson(raw: object) -> RouteGeometryOut | None:
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "LineString":
        return None
    raw_coordinates = payload.get("coordinates")
    if not isinstance(raw_coordinates, list) or len(raw_coordinates) > _MAX_GEOMETRY_POINTS:
        return None
    coordinates: list[tuple[float, float]] = []
    for pair in raw_coordinates:
        if not isinstance(pair, list) or len(pair) < 2:
            continue
        longitude, latitude = pair[0], pair[1]
        if (
            isinstance(longitude, bool)
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or not isinstance(latitude, (int, float))
            or not math.isfinite(float(longitude))
            or not math.isfinite(float(latitude))
            or not -180 <= float(longitude) <= 180
            or not -90 <= float(latitude) <= 90
        ):
            continue
        coordinates.append((float(longitude), float(latitude)))
    if len(coordinates) < 2:
        return None
    return RouteGeometryOut(coordinates=coordinates)


async def routing_snapshot_out(
    session: AsyncSession,
    snapshot_id: UUID | None,
) -> RouteExecutionRoutingOut | None:
    """Build a bounded execution DTO without exposing provider payloads."""

    if snapshot_id is None:
        return None
    snapshot = await session.get(RouteRoutingSnapshot, snapshot_id)
    if snapshot is None:
        return None
    raw_geometry = await session.scalar(
        select(ST_AsGeoJSON(RouteRoutingSnapshot.geometry)).where(
            RouteRoutingSnapshot.id == snapshot.id
        )
    )
    return RouteExecutionRoutingOut(
        snapshot_id=snapshot.id,
        revision=snapshot.revision,
        captured_at=snapshot.captured_at,
        route_updated_at=snapshot.route_updated_at,
        provider=snapshot.provider,
        provider_version=snapshot.provider_version,
        transport_mode=snapshot.transport_mode,
        geometry=_geometry_from_geojson(raw_geometry),
        distance_meters=snapshot.distance_meters,
        movement_duration_seconds=snapshot.movement_duration_seconds,
        visit_duration_minutes=snapshot.visit_duration_minutes,
        transfer_duration_seconds=snapshot.transfer_duration_seconds,
        buffer_duration_seconds=snapshot.buffer_duration_seconds,
        total_duration_seconds=snapshot.total_duration_seconds,
        elevation_gain_meters=snapshot.elevation_gain_meters,
        elevation_loss_meters=snapshot.elevation_loss_meters,
        min_altitude_meters=snapshot.min_altitude_meters,
        max_altitude_meters=snapshot.max_altitude_meters,
        max_road_angle_degrees=snapshot.max_road_angle_degrees,
        road_types=(snapshot.road_types or [])[:_MAX_ROAD_TYPE_ITEMS],
        quality_status=cast(
            RouteQualityStatus,
            snapshot.quality_status if snapshot.quality_status in _QUALITY_STATUSES else "unknown",
        ),
        quality_policy_version=snapshot.quality_policy_version,
        warnings=(snapshot.warnings or [])[:_MAX_WARNING_ITEMS],
    )
