#!/usr/bin/env python3
"""Import independent OSM coastline/trail geometry for the terrain gate.

Same free source and endpoint-fallback pattern as ``import_osm_crimea.py``
(Overpass API, ``CRIMEA_CANDIDATE_BBOX``), but fetches full way geometry
(``out geom;``) instead of POI centers, and writes to
``route_terrain_features`` instead of ``places``. This is a manual/ops
script, not a CI job — Overpass mirrors are third-party and rate-limited.

Examples:
  uv run python scripts/import_terrain_features.py --fetch --output /tmp/terrain.json
  uv run python scripts/import_terrain_features.py --input /tmp/terrain.json --apply
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from geoalchemy2 import WKTElement
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.places.application.osm_import import CRIMEA_CANDIDATE_BBOX, BoundingBox
from tourism_backend.modules.route_builder.infrastructure.models import RouteTerrainFeature

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
PRIVATE_COFFEE_OVERPASS_ENDPOINT = "https://overpass.private.coffee/api/interpreter"
VK_OVERPASS_ENDPOINT = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"

# A raw way can carry thousands of points for a long coastline segment; this
# is a quality-gate proximity check, not a rendering surface, so a coarse
# simplification is enough and keeps the table small on a memory-tight host.
_MAX_POINTS_PER_WAY = 400

_QUERIES: dict[str, str] = {
    "coastline": 'way["natural"="coastline"]',
    # "path" is OSM's dedicated-trail tag. track/footway/steps are mostly
    # farm roads and urban sidewalks across the full bbox — tens of
    # thousands of irrelevant ways that would bloat the table for no
    # safety-relevant signal.
    "trail": 'way["highway"="path"]',
}


def _now() -> datetime:
    return datetime.now(UTC)


def _build_query(bbox: BoundingBox, selector: str) -> str:
    bounds = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
    return f"""[out:json][timeout:180];
(
  {selector}({bounds});
);
out geom;"""


def _read_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("Terrain input must be a JSON object")
    return value


def _fetch_kind(
    kind: str,
    *,
    endpoints: list[str],
    bbox: BoundingBox,
) -> list[dict[str, Any]]:
    query = _build_query(bbox, _QUERIES[kind])
    failures: list[str] = []
    with httpx.Client(
        headers={"User-Agent": "CrimeaTrip-terrain-import/1.0"},
        timeout=httpx.Timeout(connect=15, read=120, write=30, pool=15),
    ) as client:
        for endpoint in endpoints:
            print(f"Overpass {kind}: requesting {endpoint}", flush=True)
            try:
                response = client.post(endpoint, data={"data": query})
                response.raise_for_status()
                value = response.json()
                elements = value.get("elements") if isinstance(value, dict) else None
                if not isinstance(elements, list):
                    raise ValueError("response has no elements array")
                print(f"Overpass {kind}: received {len(elements)} ways", flush=True)
                return [item for item in elements if isinstance(item, dict)]
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"kind={kind} endpoint={endpoint} error={type(exc).__name__}")
    raise SystemExit("All Overpass endpoints failed: " + "; ".join(failures))


def _fetch_payload(endpoints: list[str], bbox: BoundingBox) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for kind in _QUERIES:
        for element in _fetch_kind(kind, endpoints=endpoints, bbox=bbox):
            element["_kind"] = kind
            elements.append(element)
    return {"elements": elements}


def _way_to_wkt(element: dict[str, Any]) -> str | None:
    geometry = element.get("geometry")
    if not isinstance(geometry, list) or len(geometry) < 2:
        return None
    points: list[tuple[float, float]] = []
    for point in geometry:
        if not isinstance(point, dict):
            continue
        lat, lon = point.get("lat"), point.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            points.append((float(lon), float(lat)))
    if len(points) < 2:
        return None
    if len(points) > _MAX_POINTS_PER_WAY:
        step = len(points) / _MAX_POINTS_PER_WAY
        simplified = [points[int(i * step)] for i in range(_MAX_POINTS_PER_WAY)]
        if simplified[-1] != points[-1]:
            simplified.append(points[-1])
        points = simplified
    body = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in points)
    return f"LINESTRING({body})"


def apply_elements(session: Session, elements: list[dict[str, Any]]) -> tuple[int, int, int]:
    fetched_at = _now()

    # One query for all existing rows instead of a per-element SELECT — a
    # full Crimea-bbox way count can be in the tens of thousands.
    existing: dict[tuple[str, int], RouteTerrainFeature] = {
        (row.kind, row.source_osm_id): row
        for row in session.scalars(select(RouteTerrainFeature)).all()
    }

    created = 0
    updated = 0
    skipped = 0
    for element in elements:
        kind = element.get("_kind")
        osm_id = element.get("id")
        if kind not in _QUERIES or not isinstance(osm_id, int):
            skipped += 1
            continue
        wkt = _way_to_wkt(element)
        if wkt is None:
            skipped += 1
            continue
        feature = existing.get((kind, osm_id))
        if feature is None:
            feature = RouteTerrainFeature(
                id=uuid4(),
                kind=kind,
                source_osm_id=osm_id,
                created_at=fetched_at,
                updated_at=fetched_at,
            )
            session.add(feature)
            existing[(kind, osm_id)] = feature
            created += 1
        else:
            updated += 1
        feature.geometry = WKTElement(wkt, srid=4326)
        feature.fetched_at = fetched_at
        feature.updated_at = fetched_at
    return created, updated, skipped


def _write_report(path: Path, elements: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {kind: sum(1 for e in elements if e.get("_kind") == kind) for kind in _QUERIES}
    path.write_text(
        json.dumps({"input_count": len(elements), "by_kind": counts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OSM coastline/trail terrain features")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Previously downloaded Overpass JSON")
    source.add_argument("--fetch", action="store_true", help="Fetch from Overpass API")
    parser.add_argument(
        "--endpoint",
        action="append",
        help="Repeat to override the default Overpass endpoint fallback list",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional fetched-payload JSON (for --input reuse)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upsert fetched/loaded ways into route_terrain_features",
    )
    args = parser.parse_args()

    endpoints = args.endpoint or [
        VK_OVERPASS_ENDPOINT,
        DEFAULT_OVERPASS_ENDPOINT,
        PRIVATE_COFFEE_OVERPASS_ENDPOINT,
    ]
    if args.input:
        payload = _read_payload(args.input)
        elements = [item for item in payload.get("elements", []) if isinstance(item, dict)]
    else:
        payload = _fetch_payload(endpoints, CRIMEA_CANDIDATE_BBOX)
        elements = payload["elements"]
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    by_kind = {kind: sum(1 for e in elements if e.get("_kind") == kind) for kind in _QUERIES}
    print(f"Terrain fetch: total={len(elements)} by_kind={by_kind}")

    if not args.apply:
        print("Dry-run only; pass --apply to upsert into route_terrain_features")
        return

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        created, updated, skipped = apply_elements(session, elements)
        session.commit()
    print(f"Terrain apply OK: created={created} updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
