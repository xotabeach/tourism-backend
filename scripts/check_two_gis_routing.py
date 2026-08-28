#!/usr/bin/env python3
"""Sanitized two-point smoke for the 2GIS HTTP Routing API.

Never prints the API key, request URL, query string, or raw provider JSON.
This is a manual/ops probe, not a CI job: demo quota is finite.

The script constructs ``TwoGisRoutingProvider`` even when
``ROUTING_PROVIDER=stub`` so local DX can stay on the synthetic provider.

Examples:
  uv run python scripts/check_two_gis_routing.py
  uv run python scripts/check_two_gis_routing.py --configured-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tourism_backend.config import get_settings
from tourism_backend.modules.route_builder.application.route_quality import (
    assess_route_quality,
)
from tourism_backend.modules.route_builder.application.routing import (
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
)
from tourism_backend.modules.route_builder.infrastructure.two_gis_routing import (
    TwoGisRoutingProvider,
)

# Public Crimea points used by the OSRM/2GIS field-smoke notes. They are not
# secrets and must stay short enough to stay inside the demo 50 km cap.
_WALKING = (
    RouteWaypoint(lng=34.1664, lat=44.4952, label="Yalta promenade"),
    RouteWaypoint(lng=34.1436, lat=44.4678, label="Livadia Palace"),
)
_DRIVING = (
    RouteWaypoint(lng=34.1664, lat=44.4952, label="Yalta"),
    RouteWaypoint(lng=34.0558, lat=44.4199, label="Alupka Vorontsov"),
)


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def _geometry_point_count(wkt: str | None) -> int:
    if not wkt or "(" not in wkt:
        return 0
    body = wkt[wkt.find("(") + 1 : wkt.rfind(")")]
    return len([part for part in body.split(",") if part.strip()])


def _summary(result: RoutingResult, *, mode: TransportMode) -> dict[str, Any]:
    quality = assess_route_quality(result, transport_mode=mode, pace="moderate")
    return {
        "mode": mode,
        "provider": result.provider,
        "synthetic": result.synthetic,
        "distance_meters": result.total_distance_meters,
        "duration_seconds": result.total_duration_seconds,
        "geometry_points": _geometry_point_count(result.geometry_wkt),
        "has_geometry": result.geometry_wkt is not None,
        "elevation_gain_meters": result.elevation_gain_meters,
        "elevation_loss_meters": result.elevation_loss_meters,
        "min_altitude_meters": result.min_altitude_meters,
        "max_altitude_meters": result.max_altitude_meters,
        "max_road_angle_degrees": result.max_road_angle_degrees,
        "road_types": list(result.road_types),
        "warnings": list(result.warnings),
        "quality_status": quality.status,
        "quality_policy_version": quality.policy_version,
        "quality_warnings": list(quality.warnings),
    }


def _print_summary(payload: Mapping[str, Any]) -> None:
    print(
        f"{payload['mode']}: ok distance_m={payload['distance_meters']} "
        f"duration_s={payload['duration_seconds']} "
        f"geometry_points={payload['geometry_points']} "
        f"quality={payload['quality_status']}"
    )
    if payload["road_types"]:
        print(f"  road_types={payload['road_types']}")
    if payload["quality_warnings"]:
        print(f"  quality_warnings={payload['quality_warnings']}")


async def _probe(
    provider: TwoGisRoutingProvider,
    *,
    mode: TransportMode,
    waypoints: tuple[RouteWaypoint, RouteWaypoint],
    secret: str,
) -> dict[str, Any]:
    try:
        result = await provider.route(waypoints=list(waypoints), transport_mode=mode)
    except RoutingError as exc:
        raise SystemExit(
            f"{mode}: {_redact(exc.code, secret)} {_redact(exc.message, secret)}"
        ) from exc
    return _summary(result, mode=mode)


async def _run(*, configured_only: bool) -> dict[str, Any] | None:
    settings = get_settings()
    key = settings.two_gis_http_api_key
    configured = key is not None and bool(key.get_secret_value().strip())
    print(
        "two_gis_routing_smoke: "
        f"configured={str(configured).lower()} "
        f"routing_provider={settings.routing_provider} "
        f"base_host=routing.api.2gis.com"
    )
    if key is None or not key.get_secret_value().strip():
        raise SystemExit("TWO_GIS_HTTP_API_KEY (or TWO_GIS_API_KEY alias) is missing or empty")
    if configured_only:
        return None

    secret = key.get_secret_value()
    filters = tuple(
        item.strip() for item in settings.two_gis_routing_filters.split(",") if item.strip()
    )
    provider = TwoGisRoutingProvider(
        api_key=secret,
        base_url=settings.two_gis_routing_base_url,
        timeout_seconds=settings.routing_timeout_seconds,
        alternative=settings.two_gis_routing_alternative,
        max_route_meters=settings.two_gis_max_route_meters,
        filters=filters,
    )
    walking = await _probe(provider, mode="walk", waypoints=_WALKING, secret=secret)
    driving = await _probe(provider, mode="car", waypoints=_DRIVING, secret=secret)
    _print_summary(walking)
    _print_summary(driving)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "quota_note": "two provider calls; demo routing cap 50 km",
        "results": [walking, driving],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configured-only",
        action="store_true",
        help="print configured=true/false and exit without calling 2GIS",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for a sanitized JSON report (no key, no WKT)",
    )
    args = parser.parse_args()
    report = asyncio.run(_run(configured_only=args.configured_only))
    if args.output is not None:
        if report is None:
            raise SystemExit("--output requires a full smoke run, not --configured-only")
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote sanitized report: {args.output}")


if __name__ == "__main__":
    main()
