#!/usr/bin/env python3
"""Download/normalize OSM localities and optionally upsert inactive candidates.

Examples:
  uv run python scripts/import_osm_localities.py \
    --input /tmp/localities.json --output /tmp/report.json
  uv run python scripts/import_osm_localities.py --fetch --output /tmp/report.json
  uv run python scripts/import_osm_localities.py \
    --input /tmp/localities.json --output /tmp/report.json --apply
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
from tourism_backend.modules.geography.application.osm_locality_import import (
    CRIMEA_OSM_REGION_RELATION_IDS,
    OSM_LOCALITY_SOURCE_LICENSE,
    OSM_LOCALITY_SOURCE_NAME,
    OsmLocalityNormalizationResult,
    build_locality_overpass_query,
    normalize_locality_overpass_payload,
)
from tourism_backend.modules.geography.infrastructure.models import Locality, Region

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
FALLBACK_OVERPASS_ENDPOINTS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    DEFAULT_OVERPASS_ENDPOINT,
    "https://overpass.private.coffee/api/interpreter",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _read_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("OSM input must be a JSON object")
    return value


def _fetch_payload(endpoints: list[str]) -> dict[str, Any]:
    failures: list[str] = []
    with httpx.Client(
        headers={"User-Agent": "CrimeaTrip-locality-import/1.0"},
        timeout=httpx.Timeout(connect=15, read=120, write=30, pool=15),
    ) as client:
        for endpoint in endpoints:
            try:
                response = client.post(
                    endpoint,
                    data={"data": build_locality_overpass_query()},
                )
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
                    raise ValueError("response has no elements array")
                return value
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"endpoint={endpoint} error={type(exc).__name__}")
    raise SystemExit("All Overpass endpoints failed: " + "; ".join(failures))


def _name_key(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def apply_candidates(
    session: Session,
    result: OsmLocalityNormalizationResult,
) -> dict[str, int]:
    region = session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        raise SystemExit("Region crimea not found; run scripts/seed_crimea.py first")

    existing = list(session.scalars(select(Locality).where(Locality.region_id == region.id)).all())
    by_source = {
        locality.source_external_id: locality
        for locality in existing
        if locality.source_name == OSM_LOCALITY_SOURCE_NAME
        and locality.source_external_id is not None
    }
    by_name = {_name_key(locality.name): locality for locality in existing}
    counts = {"created_inactive": 0, "updated": 0, "name_conflict": 0}
    checked_at = _now()

    for candidate in result.candidates:
        locality = by_source.get(candidate.source_external_id)
        if locality is None:
            same_name = by_name.get(_name_key(candidate.name))
            if same_name is not None:
                # Never guess that two differently sourced entities are the
                # same administrative object. The report lets an editor link
                # or correct the source explicitly.
                counts["name_conflict"] += 1
                continue
            locality = Locality(
                id=uuid4(),
                region_id=region.id,
                slug=f"osm-locality-{candidate.osm_type}-{candidate.osm_id}",
                name=candidate.name,
                type=candidate.locality_type,
                status="inactive",
                created_at=checked_at,
                updated_at=checked_at,
            )
            session.add(locality)
            by_source[candidate.source_external_id] = locality
            by_name[_name_key(candidate.name)] = locality
            counts["created_inactive"] += 1
        else:
            counts["updated"] += 1
            # Active rows have passed an editorial gate: preserve their public
            # name/type/aliases while refreshing non-editorial source facts.
            if locality.status != "active":
                locality.name = candidate.name
                locality.type = candidate.locality_type
                locality.aliases = list(candidate.aliases) or None

        locality.population = candidate.population
        locality.center = _point(candidate.lng, candidate.lat)
        locality.source_name = OSM_LOCALITY_SOURCE_NAME
        locality.source_external_id = candidate.source_external_id
        locality.source_license = OSM_LOCALITY_SOURCE_LICENSE
        locality.source_url = candidate.source_url
        locality.source_checked_at = checked_at
        locality.freshness_status = "fresh"
        locality.updated_at = checked_at
    return counts


def _write_report(
    path: Path,
    result: OsmLocalityNormalizationResult,
    *,
    apply_counts: dict[str, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "warning": (
                    "OSM region membership and place tags are candidate data; "
                    "new rows stay inactive pending editorial review"
                ),
                "source_scope": {
                    "osm_region_relation_ids": list(CRIMEA_OSM_REGION_RELATION_IDS),
                    "new_row_status": "inactive",
                },
                "input_count": result.input_count,
                "accepted_count": len(result.candidates),
                "rejected": result.rejected,
                "apply_counts": apply_counts,
                "candidates": [candidate.as_dict() for candidate in result.candidates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--fetch", action="store_true")
    parser.add_argument("--endpoint", action="append")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="upsert candidates; every newly created locality remains inactive",
    )
    args = parser.parse_args()

    payload = (
        _read_payload(args.input)
        if args.input
        else _fetch_payload(args.endpoint or list(FALLBACK_OVERPASS_ENDPOINTS))
    )
    result = normalize_locality_overpass_payload(payload, limit=args.limit)
    apply_counts = None
    # Always leave a reviewable candidate report even when DB validation or
    # the transaction fails later.
    _write_report(args.output, result)
    if args.apply:
        engine = create_engine(get_settings().database_url_sync)
        with Session(engine) as session:
            apply_counts = apply_candidates(session, result)
            session.commit()
        _write_report(args.output, result, apply_counts=apply_counts)
    print(
        f"OSM locality normalize: input={result.input_count} "
        f"accepted={len(result.candidates)} rejected={sum(result.rejected.values())}"
    )
    if apply_counts is None:
        print("Dry-run only; pass --apply after reviewing the report")
    else:
        print(f"OSM locality apply OK: {apply_counts}")


if __name__ == "__main__":
    main()
