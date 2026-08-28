#!/usr/bin/env python3
"""Reconcile local places with the 2GIS Places catalog.

Dry-run by default. Never publishes a place and never prints the API key.
Apply without ``--output`` is refused so a reviewable report always exists.

Examples:
  uv run python scripts/enrich_places_2gis.py --limit 20 --output /tmp/2gis-dry.json
  uv run python scripts/enrich_places_2gis.py --limit 20 --output /tmp/2gis-dry.json --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.places.application.two_gis_enrichment import (
    ALLOWED_APPLY_FIELDS,
    PlaceProbe,
    already_enriched,
    build_apply_patch,
    decide_match,
    parse_catalog_items,
    sanitized_report_row,
)
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.places.infrastructure.two_gis_catalog import (
    TwoGisCatalogClient,
    TwoGisCatalogError,
)

_ = _geography_models

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_RADIUS_M = 250
DEFAULT_MAX_REQUESTS = 40
MAX_MAX_REQUESTS = 200
_MAX_CACHE_BYTES = 1_000_000


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def _cache_path(cache_dir: Path, *, query: str, lng: float, lat: float, radius_m: int) -> Path:
    digest = hashlib.sha256(f"{query}|{lng:.5f}|{lat:.5f}|{radius_m}".encode()).hexdigest()[:20]
    return cache_dir / f"items-{digest}.json"


def _load_places(session: Session, *, limit: int, only_missing: bool) -> list[PlaceProbe]:
    geom = cast(Place.location, Geometry)
    rows = session.execute(
        select(Place, ST_X(geom), ST_Y(geom))
        .order_by(Place.publication_status.desc(), Place.updated_at.desc())
        .limit(limit * 4 if only_missing else limit)
    ).all()
    probes: list[PlaceProbe] = []
    for place, lng, lat in rows:
        if lng is None or lat is None:
            continue
        if only_missing and already_enriched(place.source_payload):
            continue
        probes.append(
            PlaceProbe(
                place_id=place.id,
                name=place.name,
                lng=float(lng),
                lat=float(lat),
                address=place.address,
                publication_status=place.publication_status,
                source_external_id=place.source_external_id,
                data_quality_status=place.data_quality_status,
                opening_hours_raw=place.opening_hours_raw,
                source_payload=dict(place.source_payload)
                if isinstance(place.source_payload, dict)
                else None,
            )
        )
        if len(probes) >= limit:
            break
    return probes


def _search_cached(
    client: TwoGisCatalogClient,
    probe: PlaceProbe,
    *,
    radius_m: int,
    cache_dir: Path | None,
    secret: str,
) -> tuple[dict[str, Any], bool]:
    cache_path = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = _cache_path(
            cache_dir, query=probe.name, lng=probe.lng, lat=probe.lat, radius_m=radius_m
        )
        if cache_path.exists():
            try:
                raw = cache_path.read_bytes()
                if len(raw) <= _MAX_CACHE_BYTES:
                    cached = json.loads(raw.decode("utf-8"))
                    if isinstance(cached, dict):
                        return cached, True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
    payload = client.search(query=probe.name, lng=probe.lng, lat=probe.lat, radius_m=radius_m)
    if cache_path is not None:
        serialized = _redact(json.dumps(payload, ensure_ascii=False), secret)
        cache_path.write_text(serialized, encoding="utf-8")
    return payload, False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--radius-m", type=int, default=DEFAULT_RADIUS_M)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write allowed fields after a report; refused without --output",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_LIMIT:
        raise SystemExit(f"limit must be between 1 and {MAX_LIMIT}")
    if not 50 <= args.radius_m <= 2_000:
        raise SystemExit("radius-m must be between 50 and 2000")
    if not 1 <= args.max_requests <= MAX_MAX_REQUESTS:
        raise SystemExit(f"max-requests must be between 1 and {MAX_MAX_REQUESTS}")
    if args.apply and args.output is None:
        raise SystemExit("--apply requires --output so a reviewable report exists")

    settings = get_settings()
    key = settings.two_gis_http_api_key
    if key is None or not key.get_secret_value().strip():
        raise SystemExit("TWO_GIS_HTTP_API_KEY (or TWO_GIS_API_KEY alias) is missing or empty")
    secret = key.get_secret_value()
    client = TwoGisCatalogClient(
        api_key=secret,
        timeout_seconds=settings.routing_timeout_seconds,
    )
    engine = create_engine(settings.database_url_sync)
    counts = {
        "matched": 0,
        "ambiguous": 0,
        "not_found": 0,
        "skipped": 0,
        "quota_stop": 0,
        "error": 0,
    }
    rows: list[dict[str, Any]] = []
    requests_used = 0
    applied = 0
    quota_hit = False
    with Session(engine) as session:
        probes = _load_places(session, limit=args.limit, only_missing=args.only_missing)
        places_by_id: dict[UUID, Place] = {}
        if probes:
            places_by_id = {
                place.id: place
                for place in session.scalars(
                    select(Place).where(Place.id.in_([item.place_id for item in probes]))
                ).all()
            }
        for probe in probes:
            if quota_hit:
                row = {
                    "place_id": str(probe.place_id),
                    "name": probe.name,
                    "decision": "quota_stop",
                    "reason": "max_requests_or_http_429",
                    "confidence": None,
                    "distance_m": None,
                    "name_score": None,
                    "candidates": 0,
                    "provider_id": None,
                    "provider_name": None,
                }
                counts["quota_stop"] += 1
                rows.append(row)
                continue
            try:
                payload, from_cache = _search_cached(
                    client,
                    probe,
                    radius_m=args.radius_m,
                    cache_dir=args.cache_dir,
                    secret=secret,
                )
                if not from_cache:
                    requests_used += 1
            except TwoGisCatalogError as exc:
                if exc.code != "catalog_request_invalid":
                    requests_used += 1
                if exc.code == "catalog_quota_exceeded":
                    quota_hit = True
                    counts["quota_stop"] += 1
                    rows.append(
                        {
                            "place_id": str(probe.place_id),
                            "name": probe.name,
                            "decision": "quota_stop",
                            "reason": "http_429",
                            "confidence": None,
                            "distance_m": None,
                            "name_score": None,
                            "candidates": 0,
                            "provider_id": None,
                            "provider_name": None,
                        }
                    )
                    continue
                counts["error"] += 1
                rows.append(
                    {
                        "place_id": str(probe.place_id),
                        "name": probe.name,
                        "decision": "error",
                        "reason": _redact(exc.code, secret),
                        "confidence": None,
                        "distance_m": None,
                        "name_score": None,
                        "candidates": 0,
                        "provider_id": None,
                        "provider_name": None,
                    }
                )
                if requests_used >= args.max_requests:
                    quota_hit = True
                continue
            verdict = decide_match(probe, parse_catalog_items(payload))
            row = sanitized_report_row(probe, verdict)
            counts[verdict.decision] += 1
            rows.append(row)
            if args.apply:
                patch = build_apply_patch(probe, verdict)
                place = places_by_id.get(probe.place_id)
                if patch is not None and place is not None:
                    for field, value in patch.fields.items():
                        if field not in ALLOWED_APPLY_FIELDS:
                            raise SystemExit(f"refused unexpected apply field: {field}")
                        setattr(place, field, value)
                    place.source_payload = patch.payload
                    applied += 1
            if requests_used >= args.max_requests:
                quota_hit = True
        if args.apply:
            session.commit()

    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "dry_run": not args.apply,
        "limit": args.limit,
        "radius_m": args.radius_m,
        "max_requests": args.max_requests,
        "requests_used": requests_used,
        "applied": applied,
        "counts": counts,
        "results": rows,
    }
    print(
        "two_gis_place_enrichment"
        f"[{'applied' if args.apply else 'dry-run'}]: "
        f"scanned={len(rows)} requests={requests_used} "
        f"matched={counts['matched']} ambiguous={counts['ambiguous']} "
        f"not_found={counts['not_found']} skipped={counts['skipped']} "
        f"quota_stop={counts['quota_stop']} error={counts['error']}"
    )
    if args.output is not None:
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.output.write_text(serialized, encoding="utf-8")
        print(f"wrote sanitized report: {args.output}")


if __name__ == "__main__":
    main()
