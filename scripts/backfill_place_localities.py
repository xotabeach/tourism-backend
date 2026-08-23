#!/usr/bin/env python3
"""Attach places to their nearest locality via PostGIS (P0.1 of ADR-009).

Only 1.7% of imported places carried a `locality_id`, so `place_picker`
almost always fell back to `ILIKE '%city%'` over name/address — the root
cause of cross-peninsula legs ("Leg 0->1 exceeds max distance"). OSM gives
us coordinates for every place, so the association is a geometry question,
not a string question.

Assignment rule: nearest active locality *centre* within `--max-km`
(geography distance, real metres). Places farther than that from any
locality stay NULL on purpose — they are genuinely "not in a town", and a
wrong city label is worse than none for route filtering.

Does NOT publish or otherwise mutate editorial state.

Examples:
  uv run python scripts/backfill_place_localities.py
  uv run python scripts/backfill_place_localities.py --apply
  uv run python scripts/backfill_place_localities.py --apply --max-km 30
  uv run python scripts/backfill_place_localities.py --apply --reassign
"""

from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import create_engine, func, select, true
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.admin.infrastructure import models as _admin_models
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.knowledge.infrastructure import models as _knowledge_models
from tourism_backend.modules.notifications.infrastructure import (
    models as _notifications_models,
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


def _run(*, apply: bool, max_km: float, reassign: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    max_meters = max_km * 1000.0

    # Nearest active locality per place, evaluated in PostGIS. `<->` orders by
    # distance using the geography index; ST_Distance then reports real metres.
    distance = func.ST_Distance(Place.location, Locality.center)
    nearest = (
        select(
            Locality.id.label("locality_id"),
            Locality.name.label("locality_name"),
            distance.label("meters"),
        )
        .where(
            Locality.region_id == Place.region_id,
            Locality.status == "active",
            Locality.center.is_not(None),
        )
        .order_by(Place.location.op("<->")(Locality.center))
        .limit(1)
        .lateral("nearest")
    )

    stmt = (
        select(
            Place.id, Place.name, nearest.c.locality_id, nearest.c.locality_name, nearest.c.meters
        )
        .select_from(Place)
        .join(nearest, true())
    )
    if not reassign:
        stmt = stmt.where(Place.locality_id.is_(None))

    assigned: Counter[str] = Counter()
    too_far = 0
    updates: list[tuple[object, object]] = []

    with Session(engine) as session:
        for place_id, _place_name, locality_id, locality_name, meters in session.execute(stmt):
            if meters is None or meters > max_meters:
                too_far += 1
                continue
            assigned[str(locality_name)] += 1
            updates.append((place_id, locality_id))

        if apply and updates:
            for place_id, locality_id in updates:
                session.query(Place).filter(Place.id == place_id).update(
                    {Place.locality_id: locality_id},
                    synchronize_session=False,
                )
            session.commit()

        total = session.scalar(select(func.count()).select_from(Place)) or 0
        with_locality = (
            session.scalar(
                select(func.count()).select_from(Place).where(Place.locality_id.is_not(None))
            )
            or 0
        )

    mode = "applied" if apply else "dry-run"
    print(f"backfill_place_localities[{mode}]: max_km={max_km} reassign={reassign}")
    for name, count in assigned.most_common():
        print(f"  {name:20s} {count}")
    print(f"  {'(beyond max_km)':20s} {too_far}")
    print(f"  matched={len(updates)}")
    print(f"coverage now: {with_locality}/{total} ({100.0 * with_locality / max(total, 1):.1f}%)")
    if not apply:
        print("Dry-run only; pass --apply to write locality_id")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill places.locality_id via PostGIS")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--max-km",
        type=float,
        default=25.0,
        help="Maximum distance to a locality centre for association (default 25)",
    )
    parser.add_argument(
        "--reassign",
        action="store_true",
        help="Also recompute places that already have a locality_id",
    )
    args = parser.parse_args()
    if not 0 < args.max_km <= 200:
        raise SystemExit("max-km must be in (0, 200]")
    _run(apply=args.apply, max_km=args.max_km, reassign=args.reassign)


if __name__ == "__main__":
    main()
