#!/usr/bin/env python3
"""Backfill planning facts on places from OSM source_payload (Overpass tags).

Examples:
  uv run python scripts/enrich_places_facts.py
  uv run python scripts/enrich_places_facts.py --apply --limit 500
  uv run python scripts/enrich_places_facts.py --source openstreetmap --apply
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.places.application.fact_enrichment import (
    facts_from_place_payload,
    merge_fact_patch,
)
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory

_ = _geography_models


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich place facts from OSM payload")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--source", default="openstreetmap")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20_000:
        raise SystemExit("limit must be between 1 and 20000")

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    updated = 0
    scanned = 0
    with Session(engine) as session:
        places = list(
            session.scalars(
                select(Place)
                .where(
                    Place.source_name == args.source,
                    Place.source_payload.is_not(None),
                )
                .order_by(Place.updated_at.desc())
                .limit(args.limit)
            )
        )
        for place in places:
            scanned += 1
            category_codes = list(
                session.scalars(
                    select(Category.code)
                    .join(PlaceCategory, PlaceCategory.category_id == Category.id)
                    .where(PlaceCategory.place_id == place.id)
                )
            )
            patch = facts_from_place_payload(
                place.source_payload,
                category_hint=category_codes[0] if category_codes else None,
            )
            current = {
                "typical_crowding": place.typical_crowding,
                "price_min_amount": place.price_min_amount,
                "price_max_amount": place.price_max_amount,
                "price_currency": place.price_currency,
                "price_notes": place.price_notes,
                "access_transport": place.access_transport,
                "parking_available": place.parking_available,
                "seasonality": place.seasonality,
                "recommended_visit_minutes": place.recommended_visit_minutes,
                "payment_status": place.payment_status,
                "is_suitable_for_pets": place.is_suitable_for_pets,
                "accessibility": place.accessibility,
            }
            updates = merge_fact_patch(
                current=current,
                patch=patch,
                overwrite=args.overwrite,
            )
            if not updates:
                continue
            updated += 1
            if args.apply:
                for key, value in updates.items():
                    setattr(place, key, value)
                place.updated_at = datetime.now(UTC)
        if args.apply:
            session.commit()

    mode = "applied" if args.apply else "dry-run"
    print(f"enrich_places_facts[{mode}]: scanned={scanned} would_update={updated}")


if __name__ == "__main__":
    main()
