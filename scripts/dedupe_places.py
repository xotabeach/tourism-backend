#!/usr/bin/env python3
"""Merge OSM-imported duplicates of seed places (ADR-009 P0-bis 0b.1).

The review gate surfaced "Ханский дворец" showing without a photo even
though one had been imported: the DB holds it three times (seed=published,
two OSM drafts, one carrying the Wikimedia photo). 16/20 seed places have an
OSM twin within ~400m, and publishing the OSM import as-is would create
visible catalog duplicates.

Seed always wins (it has editorial text and `route_stops` already points at
it); the matched OSM place hands over its cover photo, wikidata/wikipedia
provenance and any typed field the seed is missing, then is archived — never
deleted, since `routes.place_id` is `ondelete="RESTRICT"`.

Candidates are found geometrically (`ST_DWithin`) and scored by name
(substring containment + `difflib` similarity, see
`places.application.place_dedup`); a candidate that plausibly matches more
than one seed is left alone and reported, not auto-merged.

Pipeline order (see promote_osm_fields.py / import_place_photos.py):

    import_osm_crimea.py --apply
    promote_osm_fields.py --apply
    backfill_place_localities.py --apply
    import_place_photos.py --apply
    dedupe_places.py --apply           # this script
    fetch_wikipedia_extracts.py --apply
    enrich_places_content.py --apply [--llm]

Examples:
  uv run python scripts/dedupe_places.py
  uv run python scripts/dedupe_places.py --apply
  uv run python scripts/dedupe_places.py --apply --radius-m 400 --min-similarity 0.3
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from uuid import UUID

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, aliased

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
from tourism_backend.modules.places.application.place_dedup import (
    MERGE_FIELDS,
    DuplicateCandidate,
    fields_to_copy,
    group_candidates,
    matches_candidate,
    merged_source_payload,
    name_similarity,
)
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
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


def _find_candidate_pairs(
    session: Session, *, radius_m: float
) -> list[tuple[UUID, str, DuplicateCandidate]]:
    """(seed_id, seed_name, candidate) for every OSM place within radius_m of
    a published seed place — unfiltered by name yet, so the dry-run report
    can show near-misses too."""
    seed = aliased(Place)
    candidate = aliased(Place)
    stmt = select(
        seed.id,
        seed.name,
        candidate.id,
        candidate.name,
        func.ST_Distance(seed.location, candidate.location),
    ).where(
        seed.source_name == "seed",
        seed.publication_status == "published",
        candidate.source_name == OSM_SOURCE_NAME,
        candidate.publication_status != "archived",
        candidate.region_id == seed.region_id,
        func.ST_DWithin(seed.location, candidate.location, radius_m),
    )
    return [
        (
            seed_id,
            seed_name,
            DuplicateCandidate(
                place_id=candidate_id,
                name=candidate_name,
                distance_m=float(distance_m),
                name_similarity=name_similarity(seed_name, candidate_name),
            ),
        )
        for seed_id, seed_name, candidate_id, candidate_name, distance_m in session.execute(stmt)
    ]


def _reassign_photos(session: Session, merges: dict[UUID, UUID]) -> int:
    """Move every PlaceImage from a merged-away OSM place onto its seed,
    keeping `uq_place_images_one_cover` intact (only one active cover)."""
    if not merges:
        return 0
    seed_ids = set(merges.values())
    claimed_cover: set[UUID] = set(
        session.scalars(
            select(PlaceImage.place_id).where(
                PlaceImage.place_id.in_(seed_ids),
                PlaceImage.is_cover.is_(True),
                PlaceImage.status == "active",
            )
        )
    )
    images = list(
        session.scalars(
            select(PlaceImage)
            .where(PlaceImage.place_id.in_(merges.keys()))
            .order_by(PlaceImage.place_id, PlaceImage.id)
        )
    )
    for image in images:
        seed_id = merges[image.place_id]
        if image.is_cover and image.status == "active":
            if seed_id in claimed_cover:
                image.is_cover = False
            else:
                claimed_cover.add(seed_id)
        image.place_id = seed_id
    return len(images)


def _run(*, apply: bool, radius_m: float, min_similarity: float) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)

    with Session(engine) as session:
        pairs = _find_candidate_pairs(session, radius_m=radius_m)

        seed_names: dict[UUID, str] = {}
        candidates_by_seed: dict[UUID, list[DuplicateCandidate]] = defaultdict(list)
        seed_ids_by_candidate: dict[UUID, set[UUID]] = defaultdict(set)
        for seed_id, seed_name, candidate in pairs:
            seed_names[seed_id] = seed_name
            candidates_by_seed[seed_id].append(candidate)
            if matches_candidate(seed_name, candidate, min_similarity=min_similarity):
                seed_ids_by_candidate[candidate.place_id].add(seed_id)

        merges, ambiguous = group_candidates(seed_ids_by_candidate)

        places_by_id = {
            place.id: place
            for place in session.scalars(
                select(Place).where(Place.id.in_(set(merges) | set(merges.values())))
            )
        }

        for candidate_id, seed_id in merges.items():
            osm_place = places_by_id[candidate_id]
            seed_place = places_by_id[seed_id]
            copied = fields_to_copy(
                {field: getattr(seed_place, field) for field in MERGE_FIELDS},
                {field: getattr(osm_place, field) for field in MERGE_FIELDS},
            )
            osm_tags = (osm_place.source_payload or {}).get("tags")
            new_payload = merged_source_payload(
                seed_place.source_payload,
                osm_tags if isinstance(osm_tags, dict) else {},
                osm_place.id,
            )
            if apply:
                for field, value in copied.items():
                    setattr(seed_place, field, value)
                seed_place.source_payload = new_payload
                osm_place.publication_status = "archived"
                osm_place.merged_into_place_id = seed_place.id

        moved_photos = _reassign_photos(session, merges) if apply else 0

        if apply:
            session.commit()

    mode = "applied" if apply else "dry-run"
    print(f"dedupe_places[{mode}]: radius_m={radius_m} min_similarity={min_similarity}")
    for seed_id, seed_name in sorted(seed_names.items(), key=lambda item: item[1]):
        print(f"  {seed_name!r} ({seed_id})")
        for candidate in candidates_by_seed[seed_id]:
            decision = (
                "merge"
                if merges.get(candidate.place_id) == seed_id
                else "ambiguous"
                if candidate.place_id in ambiguous
                else "no-name-match"
            )
            print(
                f"    - {candidate.name!r} dist={candidate.distance_m:.0f}m "
                f"sim={candidate.name_similarity:.2f} -> {decision}"
            )
    print(
        f"seeds_with_candidates={len(candidates_by_seed)} "
        f"merged={len(merges)} ambiguous={len(ambiguous)} photos_moved={moved_photos}"
    )
    if not apply:
        print("Dry-run only; pass --apply to archive duplicates and copy their data")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge OSM duplicates of seed places")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--radius-m",
        type=float,
        default=400.0,
        help="Max distance between seed and OSM candidate to consider (default 400)",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.9,
        help=(
            "difflib similarity threshold for auto-merge when names aren't a common "
            "prefix of each other. Kept close to exact-match: short Russian names "
            "sharing a root ('Воронцовский дворец' vs 'Воронцовский парк') can score "
            "0.78 despite being different places, and a false merge corrupts an "
            "already-published seed place, while an unmerged duplicate just waits "
            "for manual review"
        ),
    )
    args = parser.parse_args()
    if not 0 < args.radius_m <= 5000:
        raise SystemExit("radius-m must be in (0, 5000]")
    if not 0 <= args.min_similarity <= 1:
        raise SystemExit("min-similarity must be in [0, 1]")
    _run(apply=args.apply, radius_m=args.radius_m, min_similarity=args.min_similarity)


if __name__ == "__main__":
    main()
