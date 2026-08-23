#!/usr/bin/env python3
"""Import place cover photos from Wikimedia Commons (OSM `wikimedia_commons`/
`image` tags → Commons `imageinfo` API → license allowlist → local media).

Does NOT auto-publish places. Only writes `media_attachments` (role=cover)
+ `place_images` (is_cover=True); publication_status is untouched. Every
downloaded file is re-encoded to WebP and capped in size/pixels before it
touches disk (see `places.application.photo_storage`).

Mapillary is a documented follow-up (needs its own API token) — not part of
this slice; see tourism-platform/docs/progress.md.

Examples:
  uv run python scripts/import_place_photos.py --limit 50
  uv run python scripts/import_place_photos.py --apply --limit 50
  uv run python scripts/import_place_photos.py --apply --all --limit 500
"""

from __future__ import annotations

import argparse
import time

import httpx
from sqlalchemy import create_engine, exists, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Exists

from tourism_backend.config import get_settings
from tourism_backend.modules.admin.infrastructure import models as _admin_models
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.knowledge.infrastructure import models as _knowledge_models
from tourism_backend.modules.media.application.service import upsert_place_file_attachment
from tourism_backend.modules.notifications.infrastructure import (
    models as _notifications_models,
)
from tourism_backend.modules.places.application.osm_import import OSM_SOURCE_NAME
from tourism_backend.modules.places.application.photo_import import (
    WikimediaCommonsClient,
    commons_title_from_tags,
    is_license_allowed,
    normalize_wikidata_qid,
)
from tourism_backend.modules.places.application.photo_storage import (
    InvalidPlacePhoto,
    save_place_photo,
)
from tourism_backend.modules.places.application.place_images import upsert_place_image
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage
from tourism_backend.modules.route_builder.infrastructure import (
    models as _route_builder_models,
)
from tourism_backend.modules.routes.infrastructure import models as _routes_models
from tourism_backend.modules.subscriptions.infrastructure import (
    models as _subscriptions_models,
)
from tourism_backend.modules.support.infrastructure import models as _support_models

# Force full model-metadata discovery (same set as alembic/env.py) — session.commit()
# below flushes across ALL mapped classes to compute FK insert order, so a table
# referenced only via a string ForeignKey (e.g. media_attachments -> users) must
# already be registered even though this script never touches it directly.
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


def _has_active_cover_subquery() -> Exists:
    return exists().where(
        PlaceImage.place_id == Place.id,
        PlaceImage.is_cover.is_(True),
        PlaceImage.status == "active",
    )


def _run(*, apply: bool, limit: int, only_missing: bool, sleep_seconds: float) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    client = WikimediaCommonsClient()

    counts: dict[str, int] = {
        "scanned": 0,
        "no_commons_tag": 0,
        "via_wikidata": 0,
        "wikidata_no_image": 0,
        "wikidata_api_error": 0,
        "commons_not_found": 0,
        "commons_api_error": 0,
        "license_rejected": 0,
        "download_error": 0,
        "invalid_image": 0,
        "imported": 0,
        "would_import": 0,
    }

    with Session(engine) as session:
        stmt = (
            select(Place)
            .where(Place.source_name == OSM_SOURCE_NAME, Place.source_payload.is_not(None))
            .order_by(Place.updated_at.desc())
            .limit(limit)
        )
        if only_missing:
            stmt = stmt.where(~_has_active_cover_subquery())
        places = list(session.scalars(stmt))

        for place in places:
            counts["scanned"] += 1
            tags = (place.source_payload or {}).get("tags")
            if not isinstance(tags, dict):
                counts["no_commons_tag"] += 1
                continue
            title = commons_title_from_tags(tags)
            if title is None:
                qid = normalize_wikidata_qid(tags.get("wikidata"))
                if qid is None:
                    counts["no_commons_tag"] += 1
                    continue
                try:
                    title = client.fetch_commons_title_via_wikidata(qid)
                except httpx.HTTPError as exc:
                    print(f"wikidata_api_error place={place.id} qid={qid!r} error={exc!r}")
                    counts["wikidata_api_error"] += 1
                    continue
                if title is None:
                    counts["wikidata_no_image"] += 1
                    continue
                counts["via_wikidata"] += 1

            try:
                info = client.fetch_file_info(title)
            except httpx.HTTPError as exc:
                print(f"commons_api_error place={place.id} title={title!r} error={exc!r}")
                counts["commons_api_error"] += 1
                continue
            if info is None:
                counts["commons_not_found"] += 1
                continue
            if not is_license_allowed(info.license_short_name):
                print(
                    f"license_rejected place={place.id} title={title!r} "
                    f"license={info.license_short_name!r}"
                )
                counts["license_rejected"] += 1
                continue

            if not apply:
                counts["would_import"] += 1
                continue

            try:
                raw = client.download_image(info.image_url)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"download_error place={place.id} title={title!r} error={exc!r}")
                counts["download_error"] += 1
                continue
            try:
                saved = save_place_photo(raw, place_id=place.id)
            except InvalidPlacePhoto as exc:
                print(f"invalid_image place={place.id} title={title!r} error={exc!r}")
                counts["invalid_image"] += 1
                continue

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
                source_url=info.description_url,
                is_cover=True,
                author=info.artist_text,
                license=info.license_short_name,
                alt_text=place.name,
            )
            counts["imported"] += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        if apply:
            session.commit()

    mode = "applied" if apply else "dry-run"
    summary = " ".join(f"{key}={value}" for key, value in counts.items())
    print(f"import_place_photos[{mode}]: {summary}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import place photos from Wikimedia Commons")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Do not restrict to places without an active cover photo",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Politeness delay between successful downloads (Wikimedia API etiquette)",
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
