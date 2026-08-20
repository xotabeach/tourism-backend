#!/usr/bin/env python3
"""DEPRECATED mock filler: N public user_created routes per user.

Do not run on production. Match QA catalog lives in
`scripts/seed_match_catalog_routes.py` (source_name=seed_match_catalog).

Legacy invocation only:
  uv run python scripts/seed_user_routes.py --force-legacy
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from uuid import uuid4

from geoalchemy2 import WKTElement
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

ROUTES_PER_USER = 3

# Stable stop templates rotated across users (Crimea seed place slugs).
_ROUTE_TEMPLATES: list[dict[str, object]] = [
    {
        "name_prefix": "Мой маршрут",
        "short_description": "Личный маршрут путешественника по Крыму",
        "difficulty": "easy",
        "transport_mode": "walk",
        "estimated_duration_minutes": 180,
        "distance_meters": 4500,
        "stops": [
            {"place_slug": "swallow-nest", "visit_duration_minutes": 40},
            {"place_slug": "livadia-palace", "visit_duration_minutes": 60},
        ],
    },
    {
        "name_prefix": "Прогулка",
        "short_description": "Короткий авторский маршрут",
        "difficulty": "easy",
        "transport_mode": "walk",
        "estimated_duration_minutes": 150,
        "distance_meters": 3800,
        "stops": [
            {"place_slug": "ai-petri", "visit_duration_minutes": 50},
            {"place_slug": "novy-svet", "visit_duration_minutes": 45},
        ],
    },
    {
        "name_prefix": "День в Крыму",
        "short_description": "Маршрут на полдня от путешественника",
        "difficulty": "moderate",
        "transport_mode": "mixed",
        "estimated_duration_minutes": 240,
        "distance_meters": 7200,
        "stops": [
            {"place_slug": "novy-svet", "visit_duration_minutes": 55},
            {"place_slug": "chufut-kale", "visit_duration_minutes": 70},
            {"place_slug": "swallow-nest", "visit_duration_minutes": 40},
        ],
    },
]


def _now() -> datetime:
    return datetime.now(UTC)


def _linestring(points: list[tuple[float, float]]) -> WKTElement | None:
    if len(points) < 2:
        return None
    coords = ", ".join(f"{lng} {lat}" for lng, lat in points)
    return WKTElement(f"LINESTRING({coords})", srid=4326)


def _slug_for_user(user_id: object, slot: int) -> str:
    return f"user-{str(user_id).replace('-', '')[:12]}-{slot}"


def _create_route(
    *,
    session: Session,
    region: Region,
    user: User,
    places_by_slug: dict[str, Place],
    template: dict[str, object],
    slug: str,
) -> None:
    stops_payload = template["stops"]
    assert isinstance(stops_payload, list)
    for stop in stops_payload:
        assert isinstance(stop, dict)
        if stop["place_slug"] not in places_by_slug:
            raise SystemExit(f"Missing place for seed: {stop['place_slug']}")

    route = Route(
        id=uuid4(),
        region_id=region.id,
        owner_user_id=user.id,
        name=f"{template['name_prefix']}: {user.display_name}",
        slug=slug,
        short_description=str(template["short_description"]),
        description=str(template["short_description"]),
        source="user_created",
        visibility="public",
        lifecycle_status="active",
        estimated_duration_minutes=int(
            str(template["estimated_duration_minutes"]),
        ),
        distance_meters=int(str(template["distance_meters"])),
        difficulty=str(template["difficulty"]),
        transport_mode=str(template["transport_mode"]),
        is_round_trip=False,
        author_label=user.display_name,
        source_name="seed_user_routes",
        freshness_status="fresh",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(route)
    session.flush()

    for position, stop in enumerate(stops_payload, start=1):
        assert isinstance(stop, dict)
        place = places_by_slug[str(stop["place_slug"])]
        session.add(
            RouteStop(
                id=uuid4(),
                route_id=route.id,
                place_id=place.id,
                position=position,
                visit_duration_minutes=int(stop.get("visit_duration_minutes") or 40),
                is_optional=False,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    session.flush()

    coords_rows = session.execute(
        text(
            """
            SELECT ST_X(p.location::geometry) AS lng, ST_Y(p.location::geometry) AS lat
            FROM route_stops rs
            JOIN places p ON p.id = rs.place_id
            WHERE rs.route_id = :route_id
            ORDER BY rs.position
            """
        ),
        {"route_id": route.id},
    ).all()
    route.geometry = _linestring([(float(row.lng), float(row.lat)) for row in coords_rows])
    route.updated_at = _now()


def upsert_user_routes(session: Session) -> int:
    region = session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        raise SystemExit("Region crimea not found; run seed_crimea.py first")

    places_by_slug = {
        row.slug: row
        for row in session.scalars(select(Place).where(Place.region_id == region.id)).all()
    }
    users = list(session.scalars(select(User).order_by(User.created_at, User.id)).all())
    created = 0

    for user_index, user in enumerate(users):
        existing = list(
            session.scalars(
                select(Route)
                .where(
                    Route.owner_user_id == user.id,
                    Route.source == "user_created",
                    Route.visibility == "public",
                    Route.lifecycle_status == "active",
                )
                .order_by(Route.created_at, Route.id)
            ).all()
        )
        for route in existing:
            route.author_label = user.display_name
            route.updated_at = _now()

        needed = ROUTES_PER_USER - len(existing)
        if needed <= 0:
            continue

        # Prefer reserved slot slugs 1..N; fall back if occupied.
        used_slugs = {
            row.slug
            for row in session.scalars(select(Route).where(Route.region_id == region.id)).all()
        }
        next_slot = 1
        for offset in range(needed):
            template_index = (user_index + len(existing) + offset) % len(_ROUTE_TEMPLATES)
            template = _ROUTE_TEMPLATES[template_index]
            slug: str | None = None
            while next_slot < ROUTES_PER_USER + 20:
                candidate = _slug_for_user(user.id, next_slot)
                next_slot += 1
                if candidate not in used_slugs:
                    slug = candidate
                    break
            if slug is None:
                slug = f"{_slug_for_user(user.id, next_slot)}-{uuid4().hex[:6]}"
            used_slugs.add(slug)
            _create_route(
                session=session,
                region=region,
                user=user,
                places_by_slug=places_by_slug,
                template=template,
                slug=slug,
            )
            created += 1

    return created


def main() -> None:
    if "--force-legacy" not in sys.argv:
        raise SystemExit(
            "seed_user_routes.py is deprecated (created mock user routes). "
            "Use scripts/seed_match_catalog_routes.py, or pass --force-legacy."
        )
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        users_total = int(session.scalar(select(func.count()).select_from(User)) or 0)
        created = upsert_user_routes(session)
        session.commit()
        per_user = session.execute(
            text(
                """
                SELECT u.display_name, COUNT(r.id) AS route_count
                FROM users u
                LEFT JOIN routes r
                  ON r.owner_user_id = u.id
                 AND r.source = 'user_created'
                 AND r.visibility = 'public'
                 AND r.lifecycle_status = 'active'
                GROUP BY u.id, u.display_name
                ORDER BY u.created_at, u.id
                """
            )
        ).all()
        print(f"Seed user routes OK: users={users_total} routes_created={created}")
        for row in per_user:
            print(f"  - {row.display_name}: {row.route_count} routes")


if __name__ == "__main__":
    main()
