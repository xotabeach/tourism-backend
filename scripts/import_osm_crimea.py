#!/usr/bin/env python3
"""Download/normalize OSM candidates and optionally upsert them as drafts.

Examples:
  uv run python scripts/import_osm_crimea.py --input data/imports/overpass.json
  uv run python scripts/import_osm_crimea.py --fetch --limit 1000 --output /tmp/report.json
  uv run python scripts/import_osm_crimea.py --input /tmp/overpass.json --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from geoalchemy2 import WKTElement
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.application.osm_import import (
    OSM_SOURCE_LICENSE,
    OSM_SOURCE_NAME,
    OsmNormalizationResult,
    OsmPlaceCandidate,
    build_overpass_queries,
    normalize_overpass_payload,
)
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
PRIVATE_COFFEE_OVERPASS_ENDPOINT = "https://overpass.private.coffee/api/interpreter"
VK_OVERPASS_ENDPOINT = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"


def _now() -> datetime:
    return datetime.now(UTC)


def _point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _read_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("OSM input must be a JSON object")
    return value


def _batch_cache_path(cache_dir: Path, batch_index: int, query: str) -> Path:
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
    return cache_dir / f"batch-{batch_index}-{query_hash}.json"


def _fetch_payload(endpoints: list[str], cache_dir: Path | None) -> dict[str, Any]:
    elements_by_id: dict[tuple[str, int], dict[str, Any]] = {}
    failures: list[str] = []
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        headers={"User-Agent": "CrimeaTrip-place-import/1.0"},
        timeout=httpx.Timeout(connect=15, read=60, write=30, pool=15),
    ) as client:
        for batch_index, query in enumerate(build_overpass_queries(), start=1):
            cache_path = (
                _batch_cache_path(cache_dir, batch_index, query) if cache_dir is not None else None
            )
            if cache_path is not None and cache_path.exists():
                value = _read_payload(cache_path)
                print(f"Overpass batch {batch_index}/7: cache hit", flush=True)
            else:
                value = None
            for endpoint in endpoints:
                if value is not None:
                    break
                print(
                    f"Overpass batch {batch_index}/7: requesting {endpoint}",
                    flush=True,
                )
                try:
                    response = client.post(endpoint, data={"data": query})
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
                        raise ValueError("response has no elements array")
                    if cache_path is not None:
                        cache_path.write_text(
                            json.dumps(value, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    print(
                        f"Overpass batch {batch_index}/7: received "
                        f"{len(value['elements'])} elements",
                        flush=True,
                    )
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    failures.append(
                        f"batch={batch_index} endpoint={endpoint} error={type(exc).__name__}"
                    )
            else:
                raise SystemExit("All Overpass endpoints failed: " + "; ".join(failures))
            if value is None:
                raise SystemExit(f"Overpass batch {batch_index} produced no payload")
            for element in value["elements"]:
                if not isinstance(element, dict):
                    continue
                osm_type = element.get("type")
                osm_id = element.get("id")
                if isinstance(osm_type, str) and isinstance(osm_id, int):
                    elements_by_id[(osm_type, osm_id)] = element
    return {"elements": list(elements_by_id.values()), "fetch_failures": failures}


def _locality_id(candidate: OsmPlaceCandidate, localities: dict[str, Locality]) -> UUID | None:
    tags = candidate.source_payload["tags"]
    locality_name = tags.get("addr:city") or tags.get("addr:place") or tags.get("is_in:city")
    if not locality_name:
        return None
    locality = localities.get(locality_name.casefold())
    return locality.id if locality is not None else None


def _replace_categories(
    session: Session,
    place: Place,
    category_codes: tuple[str, ...],
    categories: dict[str, Category],
) -> None:
    for link in session.scalars(
        select(PlaceCategory).where(PlaceCategory.place_id == place.id)
    ).all():
        session.delete(link)
    session.flush()
    for code in category_codes:
        session.add(PlaceCategory(place_id=place.id, category_id=categories[code].id))


def apply_candidates(session: Session, result: OsmNormalizationResult) -> tuple[int, int]:
    region = session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        raise SystemExit("Region crimea not found; run scripts/seed_crimea.py first")
    category_codes = {code for item in result.candidates for code in item.category_codes}
    categories = {
        row.code: row
        for row in session.scalars(select(Category).where(Category.code.in_(category_codes))).all()
    }
    missing_categories = sorted(category_codes - categories.keys())
    if missing_categories:
        raise SystemExit(
            "Missing categories; run the current full seed first: " + ", ".join(missing_categories)
        )
    localities = {
        row.name.casefold(): row
        for row in session.scalars(select(Locality).where(Locality.region_id == region.id)).all()
    }

    created = 0
    updated = 0
    checked_at = _now()
    for candidate in result.candidates:
        place = session.scalar(
            select(Place).where(
                Place.source_name == OSM_SOURCE_NAME,
                Place.source_external_id == candidate.source_external_id,
            )
        )
        if place is None:
            place = Place(
                id=uuid4(),
                region_id=region.id,
                slug=f"osm-{candidate.osm_type}-{candidate.osm_id}",
                publication_status="draft",
                created_at=checked_at,
                updated_at=checked_at,
            )
            session.add(place)
            created += 1
        else:
            updated += 1

        place.locality_id = _locality_id(candidate, localities)
        place.name = candidate.name
        place.location = _point(candidate.lng, candidate.lat)
        place.address = candidate.address
        place.accessibility = candidate.accessibility
        place.payment_status = candidate.payment_status
        place.is_paid = candidate.payment_status == "paid"
        place.is_suitable_for_pets = candidate.is_suitable_for_pets
        place.source_name = OSM_SOURCE_NAME
        place.source_external_id = candidate.source_external_id
        place.source_url = candidate.source_url
        place.source_license = OSM_SOURCE_LICENSE
        place.source_payload = candidate.source_payload
        place.source_checked_at = checked_at
        place.freshness_status = "fresh"
        place.data_quality_status = "auto_validated"
        place.updated_at = checked_at
        session.flush()
        _replace_categories(session, place, candidate.category_codes, categories)

    return created, updated


def _write_report(path: Path, result: OsmNormalizationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "input_count": result.input_count,
                "accepted_count": len(result.candidates),
                "rejected": result.rejected,
                "candidates": [candidate.as_dict() for candidate in result.candidates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OSM Crimea place candidates")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Previously downloaded Overpass JSON")
    source.add_argument("--fetch", action="store_true", help="Fetch from Overpass API")
    parser.add_argument(
        "--endpoint",
        action="append",
        help="Repeat to override the default Overpass endpoint fallback list",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output", type=Path, help="Optional normalization report JSON")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "crimeatrip-overpass-batches",
        help="Cache successful fetch batches for resumable imports",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not persist fetched batches (recommended for ephemeral server jobs)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upsert accepted candidates into PostGIS as drafts",
    )
    args = parser.parse_args()

    endpoints = args.endpoint or [
        VK_OVERPASS_ENDPOINT,
        DEFAULT_OVERPASS_ENDPOINT,
        PRIVATE_COFFEE_OVERPASS_ENDPOINT,
    ]
    payload = (
        _read_payload(args.input)
        if args.input
        else _fetch_payload(endpoints, None if args.no_cache else args.cache_dir)
    )
    result = normalize_overpass_payload(payload, limit=args.limit)
    if args.output:
        _write_report(args.output, result)

    print(
        f"OSM normalize: input={result.input_count} accepted={len(result.candidates)} "
        f"rejected={sum(result.rejected.values())} reasons={result.rejected}"
    )
    if not args.apply:
        print("Dry-run only; pass --apply to upsert candidates as unpublished drafts")
        return

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        created, updated = apply_candidates(session, result)
        session.commit()
    print(f"OSM apply OK: created={created} updated={updated} publication_status=draft")


if __name__ == "__main__":
    main()
