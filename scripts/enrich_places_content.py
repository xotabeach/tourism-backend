#!/usr/bin/env python3
"""Draft human slug/descriptions for places (heuristic, or LM Studio via --llm).

Does NOT auto-publish. Sets content_enrichment_status=generated_draft and
fills empty short_description/description + proposed_slug. Any LLM failure
(timeout, bad JSON, provider down) falls back to the heuristic draft for that
place — the batch never aborts on a single bad response.

When `fetch_wikipedia_extracts.py` has already stored a
source_payload["wikipedia"]["extract"] for a place, both the heuristic and
the LLM draft use it as grounding instead of inventing generic filler text —
see `content_enrichment.py` / `lm_studio.py`'s grounded prompt.

Examples:
  uv run python scripts/enrich_places_content.py --limit 100
  uv run python scripts/enrich_places_content.py --apply --limit 100
  # --llm needs AI_PLANNING_ENABLED=true and AI_PROVIDER=lmstudio (home-lab
  # LM Studio reachable) — otherwise it prints why and falls back silently.
  uv run python scripts/enrich_places_content.py --apply --llm
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.places.application.content_enrichment import (
    llm_content_draft_or_fallback,
)
from tourism_backend.modules.places.application.publication_readiness import (
    meaningful_text_ignoring_status,
)
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.route_builder.infrastructure.lm_studio import LMStudioProvider

_ = _geography_models


def _lm_studio_callable(settings: Any) -> Any | None:
    """Build the `llm_callable` used for content drafts, or None if LM Studio
    is not configured — the caller then falls back to heuristic drafts."""
    if settings.ai_provider.value != "lmstudio":
        return None
    if not settings.lm_studio_base_url or not settings.lm_studio_model:
        return None
    api_key = (
        settings.lm_studio_api_key.get_secret_value()
        if settings.lm_studio_api_key is not None
        else None
    )
    provider = LMStudioProvider(
        base_url=settings.lm_studio_base_url,
        model=settings.lm_studio_model,
        api_key=api_key,
        timeout_seconds=settings.ai_request_timeout_seconds,
    )

    async def _callable(payload: dict[str, Any]) -> dict[str, Any]:
        return await provider.draft_place_content(
            name=str(payload.get("name") or ""),
            source_text=payload.get("source_text") or None,
            categories=list(payload.get("categories") or []),
            city=payload.get("city"),
        )

    return _callable


async def _run(*, apply: bool, limit: int, llm: bool, only_missing: bool) -> None:
    settings = get_settings()
    llm_callable = None
    if llm and not settings.ai_planning_enabled:
        print("AI_PLANNING_ENABLED is false; continuing with heuristic drafts only")
        llm = False
    if llm:
        llm_callable = _lm_studio_callable(settings)
        if llm_callable is None:
            print(
                "AI_PROVIDER is not lmstudio (or LM Studio base URL/model missing); "
                "continuing with heuristic drafts only"
            )
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
            wikipedia = (place.source_payload or {}).get("wikipedia")
            source_text = wikipedia.get("extract") if isinstance(wikipedia, dict) else None
            draft = await llm_content_draft_or_fallback(
                place_id=place.id,
                name=place.name,
                source_external_id=place.source_external_id,
                category_names=categories,
                city_hint=city,
                llm_enabled=llm,
                llm_callable=llm_callable,
                source_text=source_text if isinstance(source_text, str) else None,
            )
            updated += 1
            if not apply:
                continue
            # publication_readiness treats a place as already having real
            # content when EITHER field clears the meaningful-content bar on
            # its own — a short OSM survey-marker "description" (9 chars
            # median, see ADR-009) does not, even though it is non-empty and
            # therefore untouched below. Decide this *before* writing: once
            # short_description is filled in, `max(len)` could otherwise
            # pick the new machine draft as "the" content and (wrongly)
            # count it as reviewed just because content_enrichment_status
            # was left alone (this let 691 template one-liners slip past the
            # gate in production once — see the fix commit for the story).
            already_had_real_content = (
                meaningful_text_ignoring_status(
                    name=place.name,
                    short_description=place.short_description,
                    description=place.description,
                )
                is not None
            )
            place.proposed_slug = draft.proposed_slug
            if not place.short_description:
                place.short_description = draft.short_description
            if not place.description:
                place.description = draft.description
            # Promote readable slug only for still-technical OSM drafts.
            if place.slug.startswith("osm-") and place.publication_status == "draft":
                place.slug = draft.proposed_slug
            if not already_had_real_content:
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
