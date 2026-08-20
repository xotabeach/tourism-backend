#!/usr/bin/env python3
"""Draft human slug/descriptions for places (heuristic; optional LLM later).

Does NOT auto-publish. Sets content_enrichment_status=generated_draft and
fills empty short_description/description + proposed_slug.

Examples:
  uv run python scripts/enrich_places_content.py --limit 100
  uv run python scripts/enrich_places_content.py --apply --limit 100
  uv run python scripts/enrich_places_content.py --apply --llm  # needs AI flag + home lab
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.places.application.content_enrichment import (
    llm_content_draft_or_fallback,
)
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory

_ = _geography_models


async def _run(*, apply: bool, limit: int, llm: bool, only_missing: bool) -> None:
    settings = get_settings()
    if llm and not settings.ai_planning_enabled:
        print("AI_PLANNING_ENABLED is false; continuing with heuristic drafts only")
        llm = False

    engine = create_engine(settings.database_url_sync)
    scanned = 0
    updated = 0
    with Session(engine) as session:
        stmt = select(Place).order_by(Place.updated_at.desc()).limit(limit)
        if only_missing:
            stmt = stmt.where(
                or_(
                    Place.short_description.is_(None),
                    Place.description.is_(None),
                    Place.content_enrichment_status == "missing",
                    Place.slug.like("osm-%"),
                )
            )
        places = list(session.scalars(stmt))
        for place in places:
            scanned += 1
            if place.content_enrichment_status == "editorial_reviewed":
                continue
            categories = list(
                session.scalars(
                    select(Category.name)
                    .join(PlaceCategory, PlaceCategory.category_id == Category.id)
                    .where(PlaceCategory.place_id == place.id)
                )
            )
            city = None
            if place.locality_id is not None:
                locality = session.get(Locality, place.locality_id)
                city = locality.name if locality is not None else None
            draft = await llm_content_draft_or_fallback(
                place_id=place.id,
                name=place.name,
                source_external_id=place.source_external_id,
                category_names=categories,
                city_hint=city,
                llm_enabled=llm,
                llm_callable=None,  # wired when home-lab LM Studio adapter is ready
            )
            updated += 1
            if not apply:
                continue
            place.proposed_slug = draft.proposed_slug
            if not place.short_description:
                place.short_description = draft.short_description
            if not place.description:
                place.description = draft.description
            # Promote readable slug only for still-technical OSM drafts.
            if place.slug.startswith("osm-") and place.publication_status == "draft":
                place.slug = draft.proposed_slug
            place.content_enrichment_status = draft.status
            place.content_enrichment = draft.provenance
            place.updated_at = datetime.now(UTC)
        if apply:
            session.commit()

    mode = "applied" if apply else "dry-run"
    print(f"enrich_places_content[{mode}]: scanned={scanned} would_update={updated} llm={llm}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft place slug/descriptions")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--llm", action="store_true", help="Use LM Studio when configured")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Do not restrict to missing descriptions / osm-* slugs",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 20_000:
        raise SystemExit("limit must be between 1 and 20000")
    asyncio.run(
        _run(
            apply=args.apply,
            limit=args.limit,
            llm=args.llm,
            only_missing=not args.all,
        )
    )


if __name__ == "__main__":
    main()
