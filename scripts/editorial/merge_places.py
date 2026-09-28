#!/usr/bin/env python3
"""Merge published duplicates into one card (spec 16, owner's call 2026-09-28).

The first hand-made seed (Ласточкино гнездо, Ханский дворец…) and the OSM
import both published the same sights. Each group names a survivor (the
seed: routes, favourites and reviews already point at it) and its twins.
For every group:
  - the survivor gets the editorial text (gemma, from the Wikipedia source)
    and its «Источник: Википедия» line;
  - every photo of the twins moves to the survivor, skipping a photo already
    there (same Commons page); one cover stays;
  - the survivor takes the OSM location of the named twin (seed points were
    put by hand, up to 2 km off) and the twin's OSM tags for reference;
  - favourites and route stops move to the survivor;
  - the twin is archived with merged_into_place_id, never deleted.

Reads JSON lines from stdin:
  {"survivor", "twins": [...], "location_from": twin id or null,
   "text": {"short", "description", "source_title", "source_url", "model"}}

  cat groups.jsonl | docker compose exec -T backend python \\
      scripts/editorial/merge_places.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

import tourism_backend.main  # noqa: F401  (every model, for the mapper)
from tourism_backend.config import get_settings
from tourism_backend.modules.favorites.infrastructure.models import FavoritePlace
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application.publication_readiness import EDITORIAL_REVIEWED
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
from tourism_backend.modules.routes.infrastructure.models import RouteStop

WIKIPEDIA_LICENSE = "CC BY-SA 4.0"


def _merge(session: Session, group: dict[str, Any], counts: dict[str, int]) -> None:
    def bump(key: str, by: int = 1) -> None:
        counts[key] = counts.get(key, 0) + by

    survivor = session.get(Place, UUID(group["survivor"]))
    twins = [session.get(Place, UUID(t)) for t in group["twins"]]
    if survivor is None or any(t is None for t in twins):
        bump("missing")
        return
    now = datetime.now(UTC)

    text = group.get("text")
    if text:
        survivor.short_description = text["short"]
        survivor.description = text["description"]
        survivor.content_enrichment_status = EDITORIAL_REVIEWED
        survivor.content_enrichment = {
            **(survivor.content_enrichment or {}),
            "editorial": {"model": text.get("model"), "reviewed_at": now.isoformat()},
            "text_source": {
                "title": text.get("source_title"),
                "url": text.get("source_url"),
                "license": WIKIPEDIA_LICENSE,
            },
        }
        bump("text")

    source = next((t for t in twins if str(t.id) == group.get("location_from")), None)
    if source is not None:
        # Copied inside the database: reading the point into Python would
        # need shapely, which the image does not carry.
        session.execute(
            update(Place)
            .where(Place.id == survivor.id)
            .values(location=select(Place.location).where(Place.id == source.id).scalar_subquery())
            .execution_options(synchronize_session=False)
        )
        payload = dict(survivor.source_payload or {})
        payload["merged_from_osm"] = {
            **(payload.get("merged_from_osm") or {}),
            str(source.id): {"tags": (source.source_payload or {}).get("tags")},
        }
        survivor.source_payload = payload
        bump("location")

    pages = set(
        session.scalars(
            select(PlaceImage.source_url).where(
                PlaceImage.place_id == survivor.id, PlaceImage.status == "active"
            )
        )
    )
    has_cover = bool(
        session.scalar(
            select(PlaceImage.id).where(
                PlaceImage.place_id == survivor.id,
                PlaceImage.is_cover.is_(True),
                PlaceImage.status == "active",
            )
        )
    )
    order = len(pages)
    for twin in twins:
        images = session.scalars(
            select(PlaceImage)
            .where(PlaceImage.place_id == twin.id, PlaceImage.status == "active")
            .order_by(PlaceImage.is_cover.desc(), PlaceImage.sort_order)
        ).all()
        for image in images:
            if image.source_url in pages:
                image.status = "archived"
                bump("photo_duplicate")
                continue
            pages.add(image.source_url)
            image.place_id = survivor.id
            image.is_cover = image.is_cover and not has_cover
            has_cover = has_cover or image.is_cover
            image.sort_order = order
            order += 1
            image.updated_at = now
            attachment = session.get(MediaAttachment, image.media_asset_id)
            if attachment is not None:
                attachment.entity_id = survivor.id
                attachment.role = "cover" if image.is_cover else "gallery"
            bump("photo")
        for favorite in session.scalars(
            select(FavoritePlace).where(FavoritePlace.place_id == twin.id)
        ).all():
            if session.get(FavoritePlace, (favorite.user_id, survivor.id)) is None:
                session.execute(
                    update(FavoritePlace)
                    .where(
                        FavoritePlace.user_id == favorite.user_id,
                        FavoritePlace.place_id == twin.id,
                    )
                    .values(place_id=survivor.id)
                )
                bump("favorite")
            else:
                session.delete(favorite)
        stops = session.execute(
            update(RouteStop).where(RouteStop.place_id == twin.id).values(place_id=survivor.id)
        )
        bump("route_stop", stops.rowcount or 0)
        twin.publication_status = "archived"
        twin.merged_into_place_id = survivor.id
        twin.updated_at = now
        bump("archived")
    survivor.updated_at = now
    print(f"merged {survivor.name}: {len(twins)} twin(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    groups = [json.loads(line) for line in sys.stdin if line.strip()]
    engine = create_engine(get_settings().database_url_sync)
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for group in groups:
            _merge(session, group, counts)
        session.flush()
        if args.apply:
            session.commit()
        else:
            session.rollback()
    mode = "applied" if args.apply else "dry-run"
    print(f"merge_places[{mode}]: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
