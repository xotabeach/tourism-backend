#!/usr/bin/env python3
"""Import place cover photos from Wikimedia Commons (OSM `wikimedia_commons`/
`image` tags → Commons `imageinfo` API → license allowlist → local media).

Does NOT auto-publish places. Only writes `media_attachments` (role=cover)
+ `place_images` (is_cover=True); publication_status is untouched. Every
downloaded file is re-encoded to WebP and capped in size/pixels before it
touches disk (see `places.application.photo_storage`).

Mapillary is a documented follow-up (needs its own API token) — not part of
this slice; see tourism-platform/docs/progress.md.

`--geosearch` adds a third source: for a place with no OSM `wikimedia_commons`
/`image`/`wikidata` tag (the overwhelming majority — see photo_import.py's
module docstring for the OSM/Wikidata yield), look up Commons photos within
`--geosearch-radius-m` of the place's own coordinates instead. This is
structurally safer than a name/type search would be: a candidate is bounded
by real GPS distance, so a place named after a mass-produced object (a tank
or aircraft model) cannot match an unrelated museum's copy of the same
model — the exact failure found in a 2026-09-03 batch of technology
memorials that had picked up Polish/Czech air force markings and mid-flight
airshow photos with no geographic connection to Crimea at all.

Examples:
  uv run python scripts/import_place_photos.py --limit 50
  uv run python scripts/import_place_photos.py --apply --limit 50
  uv run python scripts/import_place_photos.py --apply --all --limit 500
  uv run python scripts/import_place_photos.py --apply --geosearch --limit 500
"""

from __future__ import annotations

import argparse
import time

import httpx
from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, create_engine, exists, select
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


def _run(
    *,
    apply: bool,
    limit: int,
    offset: int,
    only_missing: bool,
    sleep_seconds: float,
    geosearch: bool,
    geosearch_radius_m: int,
) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    client = WikimediaCommonsClient()

    counts: dict[str, int] = {
        "scanned": 0,
        "no_commons_tag": 0,
        "via_wikidata": 0,
        "wikidata_no_image": 0,
        "wikidata_api_error": 0,
        "via_geosearch": 0,
        "geosearch_no_candidate": 0,
        "geosearch_api_error": 0,
        "no_source": 0,
        "commons_not_found": 0,
        "commons_api_error": 0,
        "license_rejected": 0,
        "download_error": 0,
        "invalid_image": 0,
        "imported": 0,
        "would_import": 0,
    }

    with Session(engine) as session:
        # Ordered by id (stable across runs, unlike updated_at which this
        # script never touches) so --offset can page through all places in
        # memory-bounded chunks — the backend container this runs inside has
        # a 192m hard limit shared with the live app; a single 5000-row pass
        # (Place ORM rows + Pillow, imported via photo_storage) OOM-killed it.
        # Places sourced any other way (internal, wikivoyage) have no OSM
        # tags to read, but every place carries coordinates — geosearch
        # covers them too, so this query no longer filters by source_name.
        geom = cast(Place.location, Geometry)
        stmt = select(Place, ST_X(geom), ST_Y(geom)).order_by(Place.id).offset(offset).limit(limit)
        if only_missing:
            stmt = stmt.where(~_has_active_cover_subquery())
        rows = session.execute(stmt).all()

        for place, lng, lat in rows:
            counts["scanned"] += 1
            tags = (place.source_payload or {}).get("tags")
            titles: list[str] = []
            source = "tag"

            if isinstance(tags, dict):
                tag_title = commons_title_from_tags(tags)
                if tag_title is not None:
                    titles = [tag_title]

            if not titles and isinstance(tags, dict):
                qid = normalize_wikidata_qid(tags.get("wikidata"))
                if qid is not None:
                    try:
                        wikidata_title = client.fetch_commons_title_via_wikidata(qid)
                    except httpx.HTTPError as exc:
                        print(f"wikidata_api_error place={place.id} qid={qid!r} error={exc!r}")
                        counts["wikidata_api_error"] += 1
                        wikidata_title = None
                    if wikidata_title is not None:
                        titles = [wikidata_title]
                        source = "wikidata"
                        counts["via_wikidata"] += 1
                    else:
                        counts["wikidata_no_image"] += 1

            if not titles and geosearch and lat is not None and lng is not None:
                try:
                    titles = client.geosearch(
                        lat=lat, lng=lng, radius_m=geosearch_radius_m, limit=5
                    )
                except httpx.HTTPError as exc:
                    print(f"geosearch_api_error place={place.id} error={exc!r}")
                    counts["geosearch_api_error"] += 1
                    titles = []
                if titles:
                    source = "geosearch"
                else:
                    counts["geosearch_no_candidate"] += 1

            if not titles:
                counts["no_source"] += 1
                continue

            # Several candidates only matters for geosearch (tag/wikidata
            # resolve to exactly one) — a name/type search is never tried
            # here, so trying the next nearby photo on a format/license
            # failure cannot reintroduce the wrong-object risk geosearch
            # exists to avoid.
            imported = False
            for title in titles:
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
                    imported = True
                    break

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
                imported = True
                break

            if not apply or not imported:
                continue
            # `imported` is only set True once `info`/`saved` are assigned in
            # the loop above, but mypy cannot see that across the break.
            assert info is not None
            assert saved is not None

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
            if source == "geosearch":
                counts["via_geosearch"] += 1
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
        "--offset",
        type=int,
        default=0,
        help="Skip this many places (ordered by id) — page through in memory-bounded chunks",
    )
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
    parser.add_argument(
        "--geosearch",
        action="store_true",
        help=(
            "Fall back to a Commons geosearch (by the place's own coordinates) "
            "when no OSM tag or Wikidata image matched"
        ),
    )
    parser.add_argument(
        "--geosearch-radius-m",
        type=int,
        default=200,
        help="Search radius for --geosearch, in metres",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        raise SystemExit("limit must be between 1 and 5000")
    if args.sleep_seconds < 0:
        raise SystemExit("sleep-seconds must be >= 0")
    if args.offset < 0:
        raise SystemExit("offset must be >= 0")
    if not 1 <= args.geosearch_radius_m <= 10_000:
        raise SystemExit("geosearch-radius-m must be between 1 and 10000")
    _run(
        apply=args.apply,
        limit=args.limit,
        offset=args.offset,
        only_missing=not args.all,
        sleep_seconds=args.sleep_seconds,
        geosearch=args.geosearch,
        geosearch_radius_m=args.geosearch_radius_m,
    )


if __name__ == "__main__":
    main()
