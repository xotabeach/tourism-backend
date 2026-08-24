#!/usr/bin/env python3
"""Fetch Wikipedia intro extracts for OSM places (ADR-009 P0-bis 0b.2).

Real reference text for narrative-field grounding: `content_enrichment`'s
LLM path was limited to name/categories/city and explicitly forbidden from
inventing history, so its descriptions stayed generic. This script downloads
the actual Russian Wikipedia intro for places that carry an OSM `wikidata`
tag (same QID `photo_import.py` already uses for cover photos) and stores it
under `source_payload["wikipedia"]` — raw material, not applied to
`description` here (that happens in `enrich_places_content.py`, which
already never overwrites a non-empty editorial field).

Does NOT touch `description`/`content_enrichment_status` or publish
anything. Downloads over the real internet (this is an operator-run ingest
script) — the LLM itself never gets network access; it only ever sees text
this script already fetched, passed as part of a closed prompt.

Pipeline order:

    import_osm_crimea.py --apply
    promote_osm_fields.py --apply
    backfill_place_localities.py --apply
    import_place_photos.py --apply
    dedupe_places.py --apply
    fetch_wikipedia_extracts.py --apply   # this script
    enrich_places_content.py --apply [--llm]

Examples:
  uv run python scripts/fetch_wikipedia_extracts.py --limit 5
  uv run python scripts/fetch_wikipedia_extracts.py --apply --limit 300
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.admin.infrastructure import models as _admin_models
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.knowledge.infrastructure import models as _knowledge_models
from tourism_backend.modules.notifications.infrastructure import (
    models as _notifications_models,
)
from tourism_backend.modules.places.application.osm_import import OSM_SOURCE_NAME
from tourism_backend.modules.places.application.photo_import import normalize_wikidata_qid
from tourism_backend.modules.places.application.wikipedia_extract import (
    WikipediaExtractClient,
    trim_extract,
)
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.infrastructure import (
    models as _route_builder_models,
)
from tourism_backend.modules.routes.infrastructure import models as _routes_models
from tourism_backend.modules.subscriptions.infrastructure import (
    models as _subscriptions_models,
)
from tourism_backend.modules.support.infrastructure import models as _support_models

# Full model-metadata discovery (same set as alembic/env.py): commit() computes
# FK ordering across every mapped class, including tables reachable only via a
# string ForeignKey that this script never touches.
_ = (
    _admin_models,
    _favorites_models,
    _geography_models,
    _identity_models,
    _knowledge_models,
    _notifications_models,
    _route_builder_models,
    _routes_models,
    _subscriptions_models,
    _support_models,
)


def _run(*, apply: bool, limit: int, only_missing: bool, sleep_seconds: float) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    client = WikipediaExtractClient()

    counts: Counter[str] = Counter()

    with Session(engine) as session:
        stmt = (
            select(Place)
            .where(Place.source_name == OSM_SOURCE_NAME, Place.source_payload.is_not(None))
            .order_by(Place.updated_at.desc())
            .limit(limit)
        )
        places = list(session.scalars(stmt))

        for place in places:
            counts["scanned"] += 1
            payload = place.source_payload or {}
            if only_missing and isinstance(payload.get("wikipedia"), dict):
                continue
            tags = payload.get("tags")
            qid = normalize_wikidata_qid(tags.get("wikidata")) if isinstance(tags, dict) else None
            if qid is None:
                counts["no_wikidata"] += 1
                continue

            title = client.fetch_ru_title_via_wikidata(qid)
            if title is None:
                counts["no_ru_title"] += 1
                continue

            extract = client.fetch_extract(title)
            if extract is None:
                counts["no_extract"] += 1
                continue

            counts["fetched"] += 1
            if not apply:
                counts["would_fetch"] += 1
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            place.source_payload = {
                **payload,
                "wikipedia": {
                    "title": extract.title,
                    "extract": trim_extract(extract.extract),
                    "url": extract.page_url,
                    "license": extract.license,
                    "fetched_at": datetime.now(UTC).isoformat(),
                },
            }
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        if apply:
            session.commit()

    mode = "applied" if apply else "dry-run"
    summary = " ".join(f"{key}={value}" for key, value in counts.items())
    print(f"fetch_wikipedia_extracts[{mode}]: {summary}")
    if not apply:
        print("Dry-run only; pass --apply to write source_payload.wikipedia")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Wikipedia intro extracts for places")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Do not skip places that already have a stored wikipedia extract",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Politeness delay between lookups (Wikimedia API etiquette)",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        raise SystemExit("limit must be between 1 and 5000")
    if args.sleep_seconds < 0:
        raise SystemExit("sleep-seconds must be >= 0")
    _run(
        apply=args.apply,
        limit=args.limit,
        only_missing=not args.all,
        sleep_seconds=args.sleep_seconds,
    )


if __name__ == "__main__":
    main()
