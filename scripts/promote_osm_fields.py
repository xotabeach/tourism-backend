#!/usr/bin/env python3
"""Fill typed `places` columns from tags already stored in source_payload.

ADR-009 P0.3 / P0.5. `import_osm_crimea.py` keeps the full OSM tag dict in
`source_payload` but maps only a few tags onto columns, so data we already
downloaded never reached the matching engine. This is a separate pipeline
step by design, mirroring `import_place_photos.py`, which also re-reads
`source_payload`:

    import_osm_crimea.py --apply     # places + source_payload
    promote_osm_fields.py --apply    # tags -> typed columns  (this script)
    import_place_photos.py --apply   # photos
    enrich_places_content.py --apply # narrative text

Never overwrites a non-empty column: editorial values always win. Pass
--overwrite only to re-derive fields from a fresh OSM payload. Does NOT
publish or change publication_status.

Examples:
  uv run python scripts/promote_osm_fields.py
  uv run python scripts/promote_osm_fields.py --apply
  uv run python scripts/promote_osm_fields.py --apply --overwrite
  uv run python scripts/promote_osm_fields.py --apply --skip-visit-minutes
"""

from __future__ import annotations

import argparse
from collections import Counter
from uuid import UUID

from sqlalchemy import create_engine, func, select
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
from tourism_backend.modules.places.application.osm_field_promotion import (
    estimate_visit_minutes,
    parse_elevation_meters,
    promoted_description,
    promoted_opening_hours,
    promoted_phone,
    promoted_surface,
    promoted_website,
)
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceCategory,
)
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


def _categories_by_place(session: Session) -> dict[UUID, set[str]]:
    grouped: dict[UUID, set[str]] = {}
    rows = session.execute(
        select(PlaceCategory.place_id, Category.slug).join(
            Category, Category.id == PlaceCategory.category_id
        )
    ).all()
    for place_id, slug in rows:
        grouped.setdefault(place_id, set()).add(slug)
    return grouped


def _run(*, apply: bool, overwrite: bool, skip_visit_minutes: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    filled: Counter[str] = Counter()

    with Session(engine) as session:
        categories = _categories_by_place(session)
        places = list(session.scalars(select(Place).where(Place.source_payload.is_not(None))))

        for place in places:
            tags = (place.source_payload or {}).get("tags")
            if not isinstance(tags, dict):
                continue
            tags = {str(key): str(value) for key, value in tags.items()}

            candidates: list[tuple[str, object | None]] = [
                ("description", promoted_description(tags)),
                ("website_url", promoted_website(tags)),
                ("contact_phone", promoted_phone(tags)),
                ("opening_hours_raw", promoted_opening_hours(tags)),
                ("surface", promoted_surface(tags)),
                ("elevation_meters", parse_elevation_meters(tags.get("ele"))),
            ]
            if not skip_visit_minutes:
                candidates.append(
                    (
                        "recommended_visit_minutes",
                        estimate_visit_minutes(categories.get(place.id, set())),
                    )
                )

            for field, value in candidates:
                if value is None:
                    continue
                if not overwrite and getattr(place, field) is not None:
                    continue
                setattr(place, field, value)
                filled[field] += 1

        if apply:
            session.commit()

        total = session.scalar(select(func.count()).select_from(Place)) or 0
        coverage = {
            field: session.scalar(
                select(func.count()).select_from(Place).where(getattr(Place, field).is_not(None))
            )
            or 0
            for field in (
                "description",
                "website_url",
                "contact_phone",
                "opening_hours_raw",
                "surface",
                "elevation_meters",
                "recommended_visit_minutes",
            )
        }

    mode = "applied" if apply else "dry-run"
    print(f"promote_osm_fields[{mode}]: scanned={len(places)} overwrite={overwrite}")
    for field, count in filled.most_common():
        print(f"  set {field:28s} {count}")
    print("coverage now:")
    for field, count in coverage.items():
        print(f"  {field:28s} {count:5d}  {100.0 * count / max(total, 1):5.1f}%")
    if not apply:
        print("Dry-run only; pass --apply to write columns")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote OSM tags into typed place columns")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-derive fields even when already set (editorial values are lost)",
    )
    parser.add_argument(
        "--skip-visit-minutes",
        action="store_true",
        help="Do not estimate recommended_visit_minutes from categories",
    )
    args = parser.parse_args()
    _run(
        apply=args.apply,
        overwrite=args.overwrite,
        skip_visit_minutes=args.skip_visit_minutes,
    )


if __name__ == "__main__":
    main()
