#!/usr/bin/env python3
"""Idempotent seed / bulk import for geography and places.

Examples:
  uv run python scripts/seed_crimea.py
  uv run python scripts/seed_crimea.py --file data/crimea_seed.json
  uv run python scripts/seed_crimea.py --file data/extra_places.json --places-only
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from geoalchemy2 import WKTElement
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure.models import Country, Locality, Region
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceCategory,
    PlaceEntrance,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = ROOT / "data" / "crimea_seed.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _point(lng: float, lat: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def upsert_country(session: Session, payload: dict[str, Any]) -> Country:
    country = session.scalar(select(Country).where(Country.code == payload["code"]))
    if country is None:
        country = Country(id=uuid4(), created_at=_now(), updated_at=_now())
        session.add(country)
    country.code = payload["code"]
    country.slug = payload["slug"]
    country.name = payload["name"]
    country.default_locale = payload.get("default_locale", "ru")
    country.timezone = payload.get("timezone", "UTC")
    country.status = "active"
    country.freshness_status = "fresh"
    country.source_name = "seed"
    country.updated_at = _now()
    session.flush()
    return country


def upsert_region(session: Session, country: Country, payload: dict[str, Any]) -> Region:
    region = session.scalar(
        select(Region).where(Region.country_id == country.id, Region.slug == payload["slug"])
    )
    if region is None:
        region = Region(id=uuid4(), country_id=country.id, created_at=_now(), updated_at=_now())
        session.add(region)
    region.name = payload["name"]
    region.slug = payload["slug"]
    region.administrative_code = payload.get("administrative_code")
    region.timezone = payload["timezone"]
    center = payload.get("center")
    if center:
        region.center = _point(float(center["lng"]), float(center["lat"]))
    region.status = "active"
    region.freshness_status = "fresh"
    region.source_name = "seed"
    region.updated_at = _now()
    session.flush()
    return region


def upsert_localities(
    session: Session,
    region: Region,
    localities: list[dict[str, Any]],
) -> dict[str, Locality]:
    by_slug: dict[str, Locality] = {}
    for payload in localities:
        locality = session.scalar(
            select(Locality).where(
                Locality.region_id == region.id,
                Locality.slug == payload["slug"],
            )
        )
        if locality is None:
            locality = Locality(
                id=uuid4(),
                region_id=region.id,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(locality)
        locality.name = payload["name"]
        locality.slug = payload["slug"]
        locality.type = payload.get("type", "city")
        center = payload.get("center")
        if center:
            locality.center = _point(float(center["lng"]), float(center["lat"]))
        locality.status = "active"
        locality.freshness_status = "fresh"
        locality.source_name = "seed"
        locality.updated_at = _now()
        session.flush()
        by_slug[locality.slug] = locality
    return by_slug


def upsert_categories(session: Session, categories: list[dict[str, Any]]) -> dict[str, Category]:
    by_code: dict[str, Category] = {}
    for payload in categories:
        category = session.scalar(select(Category).where(Category.code == payload["code"]))
        if category is None:
            category = Category(id=uuid4(), created_at=_now(), updated_at=_now())
            session.add(category)
        category.code = payload["code"]
        category.slug = payload["slug"]
        category.name = payload["name"]
        category.description = payload.get("description")
        category.icon_key = payload.get("icon_key")
        category.sort_order = int(payload.get("sort_order", 0))
        category.status = "active"
        category.updated_at = _now()
        session.flush()
        by_code[category.code] = category
    return by_code


def upsert_places(
    session: Session,
    region: Region,
    localities: dict[str, Locality],
    categories: dict[str, Category],
    places: list[dict[str, Any]],
) -> int:
    count = 0
    for payload in places:
        place = session.scalar(
            select(Place).where(Place.region_id == region.id, Place.slug == payload["slug"])
        )
        if place is None:
            place = Place(id=uuid4(), region_id=region.id, created_at=_now(), updated_at=_now())
            session.add(place)
        locality_slug = payload.get("locality_slug")
        place.locality_id = localities[locality_slug].id if locality_slug in localities else None
        place.name = payload["name"]
        place.slug = payload["slug"]
        place.short_description = payload.get("short_description")
        place.description = payload.get("description")
        place.location = _point(float(payload["lng"]), float(payload["lat"]))
        place.address = payload.get("address")
        place.difficulty = payload.get("difficulty")
        place.is_paid = bool(payload.get("is_paid", False))
        place.price_notes = payload.get("price_notes")
        place.is_suitable_for_children = payload.get("is_suitable_for_children")
        place.recommended_equipment = payload.get("recommended_equipment")
        place.seasonality = payload.get("seasonality")
        place.safety_warnings = payload.get("safety_warnings")
        place.publication_status = payload.get("publication_status", "published")
        place.freshness_status = "fresh"
        place.source_name = "seed"
        place.updated_at = _now()
        session.flush()

        existing_links = session.scalars(
            select(PlaceCategory).where(PlaceCategory.place_id == place.id)
        ).all()
        for link in existing_links:
            session.delete(link)
        session.flush()
        for code in payload.get("categories", []):
            category = categories.get(code)
            if category is None:
                raise SystemExit(f"Unknown category code in seed: {code}")
            session.add(PlaceCategory(place_id=place.id, category_id=category.id))

        entrance = session.scalar(
            select(PlaceEntrance).where(
                PlaceEntrance.place_id == place.id,
                PlaceEntrance.is_primary.is_(True),
            )
        )
        if entrance is None:
            entrance = PlaceEntrance(
                id=uuid4(),
                place_id=place.id,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(entrance)
        entrance.name = "Основной вход"
        entrance.location = place.location
        entrance.entrance_type = "main"
        entrance.is_primary = True
        entrance.status = "active"
        entrance.freshness_status = "fresh"
        entrance.source_name = "seed"
        entrance.updated_at = _now()
        count += 1
    return count


def load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Crimea geography and places")
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED)
    parser.add_argument(
        "--places-only",
        action="store_true",
        help="Only upsert places; require existing region slug=crimea and categories",
    )
    args = parser.parse_args()
    payload = load_payload(args.file)

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)

    with Session(engine) as session:
        if args.places_only:
            region = session.scalar(select(Region).where(Region.slug == "crimea"))
            if region is None:
                raise SystemExit("Region crimea not found; run full seed first")
            country = session.get(Country, region.country_id)
            assert country is not None
            localities = {
                row.slug: row
                for row in session.scalars(
                    select(Locality).where(Locality.region_id == region.id)
                ).all()
            }
            categories = {row.code: row for row in session.scalars(select(Category)).all()}
        else:
            country = upsert_country(session, payload["country"])
            region = upsert_region(session, country, payload["region"])
            localities = upsert_localities(session, region, payload.get("localities", []))
            categories = upsert_categories(session, payload.get("categories", []))

        places_payload = payload.get("places", payload if isinstance(payload, list) else [])
        if isinstance(payload, list):
            places_payload = payload
        count = upsert_places(session, region, localities, categories, places_payload)
        session.commit()
        print(f"Seed OK: country={country.code} region={region.slug} places_upserted={count}")


if __name__ == "__main__":
    main()
