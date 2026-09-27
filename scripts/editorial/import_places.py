#!/usr/bin/env python3
"""Load an approved batch of place texts and photos (spec 16, D3, D5, D19).

Reads JSON lines from stdin, one place each, produced on the editor's
machine after the owner read the batch sample:

  {"id", "short", "description", "source_title", "source_url", "model",
   "batch", "photos": [{"title", "thumb", "page", "author", "license"}]}

For every place: the texts replace the OSM ones, the Wikipedia source is kept
for the «Источник: Википедия» line, the first photo becomes the cover (the
others are remembered for the gallery), and the place is published when the
publication gate lets it. Dry-run unless --apply.

  cat batch.jsonl | docker compose exec -T backend python \\
      scripts/editorial/import_places.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import create_engine, exists, func, select
from sqlalchemy.orm import Session

import tourism_backend.main  # noqa: F401  (every model, for the mapper)
from tourism_backend.config import get_settings
from tourism_backend.modules.media.application.service import upsert_place_file_attachment
from tourism_backend.modules.places.application.photo_storage import (
    InvalidPlacePhoto,
    save_place_photo,
)
from tourism_backend.modules.places.application.place_images import upsert_place_image
from tourism_backend.modules.places.application.publication_readiness import (
    EDITORIAL_REVIEWED,
    PlacePublicationFacts,
    publication_blockers,
)
from tourism_backend.modules.places.infrastructure.models import (
    Place,
    PlaceCategory,
    PlaceImage,
)

HEADERS = {"User-Agent": "CrimeaTripEditorial/1.0 (https://xn--h1adgncbn4e.xn--p1ai)"}
WIKIPEDIA_LICENSE = "CC BY-SA 4.0"


def _has_cover(session: Session, place_id: UUID) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    PlaceImage.place_id == place_id,
                    PlaceImage.is_cover.is_(True),
                    PlaceImage.status == "active",
                )
            )
        )
    )


def _import_cover(
    session: Session, client: httpx.Client, place: Place, photo: dict[str, Any]
) -> bool:
    response = client.get(photo["thumb"], timeout=60, follow_redirects=True)
    response.raise_for_status()
    try:
        saved = save_place_photo(response.content, place_id=place.id)
    except InvalidPlacePhoto:
        return False
    attachment = upsert_place_file_attachment(
        session,
        place_id=place.id,
        role="cover",
        public_path=saved.public_path,
        alt_text=place.name,
        status="active",
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
    )
    upsert_place_image(
        session,
        place_id=place.id,
        media_asset_id=attachment.id,
        source_url=photo.get("page"),
        is_cover=True,
        author=photo.get("author"),
        license=photo.get("license"),
        alt_text=place.name,
    )
    return True


def run(rows: list[dict[str, Any]], *, apply: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    with Session(engine) as session, httpx.Client(headers=HEADERS) as client:
        for row in rows:
            place = session.get(Place, UUID(row["id"]))
            if place is None or place.merged_into_place_id is not None:
                bump("missing")
                continue
            place.short_description = row["short"]
            place.description = row["description"]
            place.content_enrichment_status = EDITORIAL_REVIEWED
            place.content_enrichment = {
                **(place.content_enrichment or {}),
                "editorial": {
                    "batch": row.get("batch"),
                    "model": row.get("model"),
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "photos": row.get("photos") or [],
                },
                # «Источник: Википедия» under the text (spec 16, D19).
                "text_source": {
                    "title": row.get("source_title"),
                    "url": row.get("source_url"),
                    "license": WIKIPEDIA_LICENSE,
                },
            }
            photos = row.get("photos") or []
            if photos and not _has_cover(session, place.id):
                if apply:
                    try:
                        if _import_cover(session, client, place, photos[0]):
                            bump("cover")
                    except httpx.HTTPError:
                        bump("cover_download_failed")
                else:
                    bump("cover_would_import")
            categories = int(
                session.scalar(
                    select(func.count())
                    .select_from(PlaceCategory)
                    .where(PlaceCategory.place_id == place.id)
                )
                or 0
            )
            blockers = publication_blockers(
                PlacePublicationFacts(
                    name=place.name,
                    has_locality=place.locality_id is not None,
                    category_count=categories,
                    short_description=place.short_description,
                    description=place.description,
                    content_enrichment_status=place.content_enrichment_status,
                    has_cover_photo=_has_cover(session, place.id) or bool(photos),
                    temporary_closure_status=place.temporary_closure_status,
                )
            )
            if blockers:
                bump("blocked")
                print(f"blocked {place.id} {place.name}: {', '.join(blockers)}")
                continue
            if place.publication_status != "published":
                place.publication_status = "published"
                bump("published")
            else:
                bump("updated")
        if apply:
            session.commit()
        else:
            session.rollback()
    mode = "applied" if apply else "dry-run"
    print(f"import_places[{mode}]: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    run(rows, apply=args.apply)


if __name__ == "__main__":
    main()
