#!/usr/bin/env python3
"""Replace mock seed_user_routes with a match-test editorial catalog.

Idempotent:
  1) delete routes with source_name='seed_user_routes' (mock per-user fillers);
  2) upsert ~18 public editorial routes tagged source_name='seed_match_catalog'
     covering cities / duration bands / interests / pace / transport / season /
     children+pets for algorithmic match QA.

Keeps real user_created routes (source_name IS NULL / other).

Examples:
  uv run python scripts/seed_match_catalog_routes.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from geoalchemy2 import WKTElement
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

SOURCE_NAME = "seed_match_catalog"
MOCK_SOURCE_NAME = "seed_user_routes"

# Cover match form axes. Durations in minutes align with scoring bands:
# d1_2 [180,1440], d3_5 [1200,4320], d6_7 [3600,6480], d7plus [5760,20160].
# transport_mode uses match-form vocabulary: walk|car|public|mixed.
# seasonality uses UI labels: весна|лето|осень|зима.
_CATALOG: list[dict[str, Any]] = [
    {
        "slug": "match-yalta-sea-day",
        "name": "Ялта · море и дворцы за день",
        "short_description": "Спокойный день: пляж, набережная и дворцы Южного берега.",
        "description": (
            "Романтичный и спокойный маршрут по Ялте: Ливадийский дворец, "
            "Ласточкино гнездо и виды на море. Подходит для отдыха и фото."
        ),
        "estimated_duration_minutes": 360,
        "distance_meters": 22_000,
        "difficulty": "easy",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "livadia-palace", "visit_duration_minutes": 75},
            {"place_slug": "swallow-nest", "visit_duration_minutes": 50},
            {"place_slug": "vorontsov-palace", "visit_duration_minutes": 90},
        ],
    },
    {
        "slug": "match-yalta-walk-romance",
        "name": "Ялта · закат и панорамы пешком",
        "short_description": "Пешая прогулка для пары: виды, море и спокойный темп.",
        "description": (
            "Романтичная пешая прогулка у Ялты: смотровые точки, море и фото на закате без спешки."
        ),
        "estimated_duration_minutes": 210,
        "distance_meters": 6_500,
        "difficulty": "easy",
        "transport_mode": "walk",
        "seasonality": ["лето", "осень"],
        "suitable_for_children": False,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "swallow-nest", "visit_duration_minutes": 45},
            {"place_slug": "massandra-palace", "visit_duration_minutes": 70},
        ],
    },
    {
        "slug": "match-yalta-mountains-active",
        "name": "Ялта · Ай-Петри и горный день",
        "short_description": "Активный день в горах над Ялтой.",
        "description": (
            "Активный маршрут: вершина Ай-Петри, панорамы и лёгкий треккинг. "
            "Горы, природа и спорт для бодрого темпа."
        ),
        "estimated_duration_minutes": 480,
        "distance_meters": 35_000,
        "difficulty": "hard",
        "transport_mode": "mixed",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": False,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "ai-petri", "visit_duration_minutes": 120},
            {"place_slug": "massandra-palace", "visit_duration_minutes": 60},
            {"place_slug": "swallow-nest", "visit_duration_minutes": 40},
        ],
    },
    {
        "slug": "match-yalta-long-rest",
        "name": "Ялта · три дня у моря",
        "short_description": "Многодневный отдых: пляж, дворцы и спокойные прогулки.",
        "description": (
            "Три дня спокойного отдыха в Ялте: море, пляж, дворцы и вино Массандры без гонки."
        ),
        "estimated_duration_minutes": 2_400,
        "distance_meters": 55_000,
        "difficulty": "easy",
        "transport_mode": "car",
        "seasonality": ["лето"],
        "suitable_for_children": True,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "livadia-palace", "visit_duration_minutes": 80},
            {"place_slug": "vorontsov-palace", "visit_duration_minutes": 90},
            {"place_slug": "massandra-palace", "visit_duration_minutes": 70},
            {"place_slug": "swallow-nest", "visit_duration_minutes": 45},
        ],
    },
    {
        "slug": "match-sevastopol-history",
        "name": "Севастополь · история и Херсонес",
        "short_description": "Исторический день: Херсонес, Сапун-гора и бухта.",
        "description": (
            "История Севастополя: античный Херсонес, Сапун-гора и виды Балаклавской бухты."
        ),
        "estimated_duration_minutes": 420,
        "distance_meters": 40_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "khersones", "visit_duration_minutes": 100},
            {"place_slug": "sapun-mountain", "visit_duration_minutes": 70},
            {"place_slug": "balaklava-bay", "visit_duration_minutes": 60},
        ],
    },
    {
        "slug": "match-sevastopol-bay-walk",
        "name": "Севастополь · бухта пешком",
        "short_description": "Спокойная пешая прогулка у Балаклавы.",
        "description": (
            "Спокойный отдых у моря: набережная и бухта Балаклавы, фото и короткая прогулка."
        ),
        "estimated_duration_minutes": 180,
        "distance_meters": 4_500,
        "difficulty": "easy",
        "transport_mode": "walk",
        "seasonality": ["лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "balaklava-bay", "visit_duration_minutes": 90},
            {"place_slug": "khersones", "visit_duration_minutes": 60},
        ],
    },
    {
        "slug": "match-bakhchisaray-heritage",
        "name": "Бахчисарай · дворец и Чуфут-Кале",
        "short_description": "История и пещерный город за день.",
        "description": (
            "Исторический маршрут: Ханский дворец и пещерный город Чуфут-Кале. "
            "Приключение средней сложности."
        ),
        "estimated_duration_minutes": 360,
        "distance_meters": 14_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "khan-palace", "visit_duration_minutes": 90},
            {"place_slug": "chufut-kale", "visit_duration_minutes": 120},
        ],
    },
    {
        "slug": "match-bakhchisaray-adventure",
        "name": "Бахчисарай · тропа к скалам",
        "short_description": "Активное приключение: скалы, пещера и тропа.",
        "description": (
            "Приключение и экстрим-лайт: скалы Чуфут-Кале, тропа и виды. Активный темп."
        ),
        "estimated_duration_minutes": 450,
        "distance_meters": 18_000,
        "difficulty": "hard",
        "transport_mode": "walk",
        "seasonality": ["весна", "осень"],
        "suitable_for_children": False,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "chufut-kale", "visit_duration_minutes": 150},
            {"place_slug": "khan-palace", "visit_duration_minutes": 60},
        ],
    },
    {
        "slug": "match-sudak-fortress-sea",
        "name": "Судак · крепость и Новый Свет",
        "short_description": "Море, крепость и тропа Голицына.",
        "description": (
            "Пляж и история: Генуэзская крепость, Новый Свет и природа восточного берега."
        ),
        "estimated_duration_minutes": 480,
        "distance_meters": 32_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "sudak-fortress", "visit_duration_minutes": 90},
            {"place_slug": "novy-svet", "visit_duration_minutes": 120},
        ],
    },
    {
        "slug": "match-sudak-adventure-week",
        "name": "Судак · неделя приключений",
        "short_description": "Неделя: крепость, тропы, Кара-Даг и море.",
        "description": (
            "Длинное приключение: Генуэзская крепость, тропа Голицына, "
            "вулканический Кара-Даг, природа и экстрим."
        ),
        "estimated_duration_minutes": 7_200,
        "distance_meters": 120_000,
        "difficulty": "hard",
        "transport_mode": "mixed",
        "seasonality": ["лето"],
        "suitable_for_children": False,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "sudak-fortress", "visit_duration_minutes": 90},
            {"place_slug": "novy-svet", "visit_duration_minutes": 150},
            {"place_slug": "kara-dag", "visit_duration_minutes": 180},
        ],
    },
    {
        "slug": "match-alushta-nature",
        "name": "Алушта · природа и Демерджи",
        "short_description": "Природа, лес и Долина привидений.",
        "description": (
            "Природный день в Алуште: Долина привидений, скалы и спокойный променад у моря."
        ),
        "estimated_duration_minutes": 390,
        "distance_meters": 28_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "demerdzhi", "visit_duration_minutes": 120},
            {"place_slug": "alushta-promenade", "visit_duration_minutes": 60},
        ],
    },
    {
        "slug": "match-alushta-beach-family",
        "name": "Алушта · пляж с детьми",
        "short_description": "Семейный день: набережная, пляж и спокойный темп.",
        "description": (
            "Семейный отдых у моря в Алуште: пляж, набережная, можно с детьми и питомцами."
        ),
        "estimated_duration_minutes": 240,
        "distance_meters": 5_000,
        "difficulty": "easy",
        "transport_mode": "walk",
        "seasonality": ["лето"],
        "suitable_for_children": True,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "alushta-promenade", "visit_duration_minutes": 120},
            {"place_slug": "demerdzhi", "visit_duration_minutes": 60},
        ],
    },
    {
        "slug": "match-evpatoria-embankment",
        "name": "Евпатория · набережная и отдых",
        "short_description": "Спокойный курортный день у моря.",
        "description": (
            "Отдых и релакс в Евпатории: набережная, пляж и спокойный темп для всей семьи."
        ),
        "estimated_duration_minutes": 300,
        "distance_meters": 8_000,
        "difficulty": "easy",
        "transport_mode": "public",
        "seasonality": ["лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "evpatoria-embankment", "visit_duration_minutes": 120},
            {"place_slug": "tarkhankut", "visit_duration_minutes": 90},
        ],
    },
    {
        "slug": "match-tarkhankut-photo",
        "name": "Тарханкут · фото и скалы",
        "short_description": "Смотровые скалы, панорамы и фотосессия.",
        "description": (
            "Фото-маршрут на мыс Тарханкут: скалы, панорамы моря и природа. Активный темп."
        ),
        "estimated_duration_minutes": 420,
        "distance_meters": 45_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": False,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "tarkhankut", "visit_duration_minutes": 150},
            {"place_slug": "evpatoria-embankment", "visit_duration_minutes": 45},
        ],
    },
    {
        "slug": "match-feodosia-art-sea",
        "name": "Феодосия · галерея и море",
        "short_description": "Искусство Айвазовского и берег.",
        "description": (
            "Культура и море: галерея Айвазовского, спокойная прогулка и отдых у Феодосии."
        ),
        "estimated_duration_minutes": 330,
        "distance_meters": 12_000,
        "difficulty": "easy",
        "transport_mode": "walk",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "feodosia-gallery", "visit_duration_minutes": 90},
            {"place_slug": "kara-dag", "visit_duration_minutes": 90},
        ],
    },
    {
        "slug": "match-feodosia-kara-dag-week",
        "name": "Феодосия · Кара-Даг на неделю",
        "short_description": "Длинный природный маршрут восточного берега.",
        "description": (
            "Неделя природы и приключений: Кара-Даг, тропы, море и спокойные вечера в Феодосии."
        ),
        "estimated_duration_minutes": 6_000,
        "distance_meters": 90_000,
        "difficulty": "moderate",
        "transport_mode": "mixed",
        "seasonality": ["лето", "осень"],
        "suitable_for_children": False,
        "pets_allowed": True,
        "stops": [
            {"place_slug": "kara-dag", "visit_duration_minutes": 180},
            {"place_slug": "feodosia-gallery", "visit_duration_minutes": 70},
            {"place_slug": "novy-svet", "visit_duration_minutes": 120},
        ],
    },
    {
        "slug": "match-simferopol-history-cave",
        "name": "Симферополь · Неаполь и пещера",
        "short_description": "История и природа вокруг столицы.",
        "description": (
            "Исторический день: Неаполь Скифский и Мраморная пещера. Подходит на 1–2 дня."
        ),
        "estimated_duration_minutes": 390,
        "distance_meters": 50_000,
        "difficulty": "easy",
        "transport_mode": "car",
        "seasonality": ["весна", "осень", "зима"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "simferopol-scythian-naples", "visit_duration_minutes": 90},
            {"place_slug": "marble-cave", "visit_duration_minutes": 100},
        ],
    },
    {
        "slug": "match-crimea-grand-tour",
        "name": "Крым · большой круг на 6–7 дней",
        "short_description": "Ялта, Бахчисарай, Судак — обзорный тур.",
        "description": (
            "Длинный обзорный маршрут: дворцы Ялты, история Бахчисарая, "
            "крепость Судака, природа и море."
        ),
        "estimated_duration_minutes": 5_400,
        "distance_meters": 280_000,
        "difficulty": "moderate",
        "transport_mode": "car",
        "seasonality": ["весна", "лето", "осень"],
        "suitable_for_children": True,
        "pets_allowed": False,
        "stops": [
            {"place_slug": "livadia-palace", "visit_duration_minutes": 70},
            {"place_slug": "khan-palace", "visit_duration_minutes": 80},
            {"place_slug": "sudak-fortress", "visit_duration_minutes": 90},
            {"place_slug": "novy-svet", "visit_duration_minutes": 100},
            {"place_slug": "khersones", "visit_duration_minutes": 80},
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


def delete_mock_user_routes(session: Session) -> int:
    routes = list(session.scalars(select(Route).where(Route.source_name == MOCK_SOURCE_NAME)).all())
    if not routes:
        return 0
    for route in routes:
        session.delete(route)
    session.flush()
    return len(routes)


def upsert_catalog(session: Session) -> tuple[int, int]:
    region = session.scalar(select(Region).where(Region.slug == "crimea"))
    if region is None:
        raise SystemExit("Region crimea not found")

    places_by_slug = {
        row.slug: row
        for row in session.scalars(select(Place).where(Place.region_id == region.id)).all()
    }
    created = 0
    updated = 0
    for payload in _CATALOG:
        for stop in payload["stops"]:
            if stop["place_slug"] not in places_by_slug:
                raise SystemExit(f"Missing place slug: {stop['place_slug']}")

        route = session.scalar(
            select(Route).where(Route.region_id == region.id, Route.slug == payload["slug"])
        )
        if route is None:
            route = Route(
                id=uuid4(),
                region_id=region.id,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(route)
            created += 1
        else:
            updated += 1

        route.name = payload["name"]
        route.slug = payload["slug"]
        route.short_description = payload["short_description"]
        route.description = payload["description"]
        route.source = "editorial"
        route.visibility = "public"
        route.lifecycle_status = "active"
        route.publication_status = "published"
        route.estimated_duration_minutes = payload["estimated_duration_minutes"]
        route.distance_meters = payload["distance_meters"]
        route.difficulty = payload["difficulty"]
        route.transport_mode = payload["transport_mode"]
        route.seasonality = payload["seasonality"]
        route.suitable_for_children = payload["suitable_for_children"]
        route.pets_allowed = payload["pets_allowed"]
        route.is_round_trip = False
        route.owner_user_id = None
        route.author_label = "КрымТрип · каталог подбора"
        route.source_name = SOURCE_NAME
        route.freshness_status = "fresh"
        route.accessibility = {
            "match_catalog": True,
            "seed": SOURCE_NAME,
        }
        route.updated_at = _now()
        session.flush()

        for stop in list(session.scalars(select(RouteStop).where(RouteStop.route_id == route.id))):
            session.delete(stop)
        session.flush()

        for position, stop in enumerate(payload["stops"], start=1):
            place = places_by_slug[stop["place_slug"]]
            session.add(
                RouteStop(
                    id=uuid4(),
                    route_id=route.id,
                    place_id=place.id,
                    position=position,
                    visit_duration_minutes=int(stop["visit_duration_minutes"]),
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

    return created, updated


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        deleted = delete_mock_user_routes(session)
        created, updated = upsert_catalog(session)
        session.commit()

        catalog_count = int(
            session.execute(
                text("SELECT COUNT(*) FROM routes WHERE source_name = :s"),
                {"s": SOURCE_NAME},
            ).scalar()
            or 0
        )
        real_user = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM routes
                    WHERE source = 'user_created'
                      AND visibility = 'public'
                      AND publication_status = 'published'
                      AND COALESCE(source_name, '') <> :mock
                    """
                ),
                {"mock": MOCK_SOURCE_NAME},
            ).scalar()
            or 0
        )
        mock_left = int(
            session.execute(
                text("SELECT COUNT(*) FROM routes WHERE source_name = :s"),
                {"s": MOCK_SOURCE_NAME},
            ).scalar()
            or 0
        )
        print(
            f"OK deleted_mock={deleted} catalog_created={created} "
            f"catalog_updated={updated} catalog_total={catalog_count} "
            f"real_user_public={real_user} mock_left={mock_left}"
        )


if __name__ == "__main__":
    main()
