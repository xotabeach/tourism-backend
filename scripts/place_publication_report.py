#!/usr/bin/env python3
"""Triage report: what blocks place publication, and where the ready ones are.

The review gate (`places.application.publication_readiness`) is mechanical,
but an editor still has to approve. With thousands of draft places nobody
clicks through them one by one, so this reports which batches are worth
opening in /admin — by locality and by blocker — before any human time is
spent.

Read-only. Publication happens in /admin, where an admin principal is
recorded in the audit log; this script deliberately cannot publish.

Examples:
  uv run python scripts/place_publication_report.py
  uv run python scripts/place_publication_report.py --status draft --limit-samples 5
"""

from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.places.application.publication_readiness import (
    PlacePublicationFacts,
    publication_blockers,
    publication_warnings,
)
from tourism_backend.modules.places.infrastructure.models import (
    Place,
    PlaceCategory,
    PlaceImage,
)


def _run(*, status: str, limit_samples: int) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)

    with Session(engine) as session:
        places = list(session.scalars(select(Place).where(Place.publication_status == status)))
        place_ids = [place.id for place in places]
        if not place_ids:
            print(f"No places with publication_status={status!r}")
            return

        counts = {
            row[0]: int(row[1])
            for row in session.execute(
                select(PlaceCategory.place_id, func.count()).group_by(PlaceCategory.place_id)
            ).all()
        }
        covered = set(
            session.scalars(
                select(PlaceImage.place_id).where(
                    PlaceImage.is_cover.is_(True), PlaceImage.status == "active"
                )
            ).all()
        )
        locality_names = {
            row[0]: row[1] for row in session.execute(select(Locality.id, Locality.name)).all()
        }

        blocker_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        ready_by_locality: Counter[str] = Counter()
        ready_samples: list[str] = []
        ready_total = 0

        for place in places:
            facts = PlacePublicationFacts(
                name=place.name,
                has_locality=place.locality_id is not None,
                category_count=counts.get(place.id, 0),
                short_description=place.short_description,
                description=place.description,
                content_enrichment_status=place.content_enrichment_status,
                has_cover_photo=place.id in covered,
                temporary_closure_status=place.temporary_closure_status,
            )
            blockers = publication_blockers(facts)
            if blockers:
                for blocker in blockers:
                    blocker_counts[blocker] += 1
                continue
            ready_total += 1
            ready_by_locality[locality_names.get(place.locality_id, "—")] += 1
            for warning in publication_warnings(facts):
                warning_counts[warning] += 1
            if len(ready_samples) < limit_samples:
                ready_samples.append(place.name)

    print(f"Place publication report (status={status}, total={len(places)})")
    print(f"\nREADY TO PUBLISH: {ready_total}")
    for name, count in ready_by_locality.most_common():
        print(f"  {name:20s} {count}")
    if ready_samples:
        print("  examples: " + "; ".join(ready_samples))
    if warning_counts:
        print("\n  warnings on ready places (do not block):")
        for warning, count in warning_counts.most_common():
            print(f"    {warning:52s} {count}")

    print(f"\nBLOCKED: {len(places) - ready_total}")
    for blocker, count in blocker_counts.most_common():
        print(f"  {blocker:52s} {count}")
    print("\nPublish from /admin (Места → выбрать → «Опубликовать»); the gate re-checks there.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report place publication readiness")
    parser.add_argument("--status", default="draft", help="publication_status to inspect")
    parser.add_argument("--limit-samples", type=int, default=8)
    args = parser.parse_args()
    _run(status=args.status, limit_samples=max(0, args.limit_samples))


if __name__ == "__main__":
    main()
