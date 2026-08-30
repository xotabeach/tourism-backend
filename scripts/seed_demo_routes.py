#!/usr/bin/env python3
"""Seed a spread of demo routes authored by an existing user.

The test contour had 27 routes, most of them near-identical two-stop pairs,
which made the recommendation deck monotonous and left the diversity ranker
nothing to balance. These add variety across difficulty, transport, stop
count, season and audience, all built from already-published places.

Every route is kept under the 2GIS demo key's 50 km cap so road geometry can
be filled in afterwards with ``backfill_route_geometry.py``.

Dry-run by default.

Examples:
  uv run python scripts/seed_demo_routes.py --author "Борода"
  uv run python scripts/seed_demo_routes.py --author "Борода" --apply
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure import models as _places_models
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.recommendations.infrastructure import models as _reco_models
from tourism_backend.modules.route_execution.infrastructure import models as _execution_models
from tourism_backend.modules.routes.infrastructure import models as _routes_models
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

_ = (
    _reco_models,
    _geography_models,
    _identity_models,
    _places_models,
    _routes_models,
    _favorites_models,
    _execution_models,
)


@dataclass(frozen=True, slots=True)
class DemoRoute:
    slug: str
    name: str
    description: str
    stops: tuple[str, ...]
    difficulty: str
    transport_mode: str
    duration_minutes: int
    suitable_for_children: bool | None = None
    pets_allowed: bool | None = None
    seasonality: tuple[str, ...] = ()
    is_round_trip: bool = False
    typical_crowding: str = "unknown"
    filters: tuple[str, ...] = field(default_factory=tuple)


# Stops are place names; every pair below is well under the 50 km demo cap.
DEMO_ROUTES: tuple[DemoRoute, ...] = (
    DemoRoute(
        slug="demo-yalta-palaces-day",
        name="Три дворца Южного берега",
        description=(
            "Спокойный день по дворцам Ялты: Ливадия, Ласточкино гнездо и "
            "Массандра. Много тени, короткие переезды, подойдёт с детьми."
        ),
        stops=("Ливадийский дворец", "Ласточкино гнездо", "Массандровский дворец"),
        difficulty="easy",
        transport_mode="car",
        duration_minutes=300,
        suitable_for_children=True,
        pets_allowed=True,
        seasonality=("весна", "лето", "осень"),
        typical_crowding="high",
        filters=("Дворцы", "Семейный"),
    ),
    DemoRoute(
        slug="demo-ai-petri-climb",
        name="Подъём на Ай-Петри",
        description=(
            "Дворцовый парк внизу и плато наверху. Перепад высот серьёзный, "
            "нужна обувь с протектором и запас воды."
        ),
        stops=("Воронцовский дворец", "Ай-Петри"),
        difficulty="hard",
        transport_mode="car",
        duration_minutes=420,
        suitable_for_children=False,
        seasonality=("лето", "осень"),
        typical_crowding="medium",
        filters=("Горы", "Панорама"),
    ),
    DemoRoute(
        slug="demo-yalta-seaside-walk",
        name="Пешком вдоль моря к Ласточкину гнезду",
        description=(
            "Прогулочный маршрут по верхней дороге с выходом к смотровой у "
            "Ласточкина гнезда. Идти лучше утром, пока не жарко."
        ),
        stops=("Ливадийский дворец", "Ласточкино гнездо"),
        difficulty="moderate",
        transport_mode="walking",
        duration_minutes=240,
        suitable_for_children=True,
        pets_allowed=True,
        seasonality=("весна", "осень"),
        filters=("Пешком", "Море"),
    ),
    DemoRoute(
        slug="demo-sevastopol-bays",
        name="Севастополь: бухта и высоты",
        description=(
            "Балаклавская бухта и панорама Сапун-горы. Немного ходьбы, "
            "основное время — виды и музейная часть."
        ),
        stops=("Балаклавская бухта", "Сапун-гора"),
        difficulty="easy",
        transport_mode="car",
        duration_minutes=270,
        suitable_for_children=True,
        seasonality=("весна", "лето", "осень"),
        filters=("История", "Панорама"),
    ),
    DemoRoute(
        slug="demo-sevastopol-antique",
        name="Античный Севастополь пешком",
        description=(
            "Херсонес и подъём к Сапун-горе. Длинный пеший день по городу, "
            "почти без тени во второй половине."
        ),
        stops=("Херсонес Таврический", "Сапун-гора"),
        difficulty="moderate",
        transport_mode="walking",
        duration_minutes=360,
        suitable_for_children=False,
        seasonality=("весна", "осень"),
        filters=("История", "Пешком"),
    ),
    DemoRoute(
        slug="demo-bakhchisaray-cave-city",
        name="Пещерный город за полдня",
        description=(
            "Ханский дворец и подъём в Чуфут-Кале. Тропа каменистая, "
            "но короткая — реально пройти с подростками."
        ),
        stops=("Ханский дворец", "Чуфут-Кале"),
        difficulty="moderate",
        transport_mode="walking",
        duration_minutes=300,
        suitable_for_children=True,
        seasonality=("весна", "лето", "осень"),
        filters=("История", "Горы"),
    ),
    DemoRoute(
        slug="demo-sudak-novy-svet",
        name="Судак и тропа Голицына",
        description=(
            "Генуэзская крепость, затем можжевеловая роща и тропа над морем. "
            "В шторм тропу закрывают — проверяйте перед выходом."
        ),
        stops=("Генуэзская крепость", "Новый Свет и тропа Голицына"),
        difficulty="moderate",
        transport_mode="walking",
        duration_minutes=330,
        pets_allowed=False,
        seasonality=("лето", "осень"),
        typical_crowding="high",
        filters=("Море", "Тропы"),
    ),
    DemoRoute(
        slug="demo-alushta-sea-and-hills",
        name="Алушта: от гор к набережной",
        description=(
            "Долина привидений с причудливыми скалами, потом спуск к морю и "
            "неспешная набережная. Хороший вариант первого дня."
        ),
        stops=("Долина привидений", "Променад Алушты"),
        difficulty="easy",
        transport_mode="car",
        duration_minutes=280,
        suitable_for_children=True,
        pets_allowed=True,
        seasonality=("лето",),
        filters=("Семейный", "Море"),
    ),
    DemoRoute(
        slug="demo-marble-cave-demerdzhi",
        name="Мраморная пещера и Демерджи",
        description=(
            "Подземные залы Мраморной пещеры и каменные фигуры Демерджи. "
            "В пещере прохладно круглый год — берите кофту."
        ),
        stops=("Мраморная пещера", "Долина привидений"),
        difficulty="moderate",
        transport_mode="car",
        duration_minutes=360,
        suitable_for_children=True,
        seasonality=("весна", "лето", "осень", "зима"),
        filters=("Пещеры", "Горы"),
    ),
    DemoRoute(
        slug="demo-simferopol-scythians",
        name="Скифы и подземелья",
        description=(
            "Неаполь Скифский на окраине Симферополя и переезд к Мраморной "
            "пещере. Много истории и один длинный переезд."
        ),
        stops=("Неаполь Скифский", "Мраморная пещера"),
        difficulty="moderate",
        transport_mode="car",
        duration_minutes=330,
        seasonality=("весна", "осень"),
        filters=("История", "Пещеры"),
    ),
    DemoRoute(
        slug="demo-feodosia-karadag",
        name="Феодосия и Кара-Даг",
        description=(
            "Галерея Айвазовского, затем вулканический массив Кара-Даг. "
            "На заповедную часть нужен сопровождающий."
        ),
        stops=("Галерея Айвазовского", "Кара-Даг"),
        difficulty="hard",
        transport_mode="car",
        duration_minutes=420,
        suitable_for_children=False,
        pets_allowed=False,
        seasonality=("лето", "осень"),
        filters=("Музеи", "Горы"),
    ),
    DemoRoute(
        slug="demo-yalta-parks-walk",
        name="Парки Ялты неспешно",
        description=(
            "Воронцовский парк и дорога к Ливадии. Ровный темп, тень и "
            "лавочки — маршрут для медленной прогулки."
        ),
        stops=("Воронцовский дворец", "Ливадийский дворец"),
        difficulty="easy",
        transport_mode="walking",
        duration_minutes=210,
        suitable_for_children=True,
        pets_allowed=True,
        seasonality=("весна", "лето", "осень"),
        filters=("Парки", "Пешком"),
    ),
)


async def _run(*, author: str, apply: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    created = 0
    skipped = 0
    try:
        async with factory() as session:
            user = await _resolve_author(session, author)
            print(f"author: {user.display_name} ({user.id})")

            places = {
                name: (place_id, region_id)
                for place_id, name, region_id in (
                    await session.execute(
                        select(Place.id, Place.name, Place.region_id).where(
                            Place.publication_status == "published"
                        )
                    )
                ).all()
            }

            now = datetime.now(UTC)
            for demo in DEMO_ROUTES:
                existing = await session.scalar(select(Route).where(Route.slug == demo.slug))
                if existing is not None:
                    skipped += 1
                    print(f"  skip (exists): {demo.name}")
                    continue
                missing = [name for name in demo.stops if name not in places]
                if missing:
                    skipped += 1
                    print(f"  skip (missing places {missing}): {demo.name}")
                    continue

                region_id = places[demo.stops[0]][1]
                route = Route(
                    id=uuid4(),
                    region_id=region_id,
                    owner_user_id=user.id,
                    name=demo.name,
                    slug=demo.slug,
                    short_description=demo.description[:240],
                    description=demo.description,
                    source="user_created",
                    visibility="public",
                    lifecycle_status="active",
                    publication_status="published",
                    estimated_duration_minutes=demo.duration_minutes,
                    difficulty=demo.difficulty,
                    transport_mode=demo.transport_mode,
                    is_round_trip=demo.is_round_trip,
                    suitable_for_children=demo.suitable_for_children,
                    pets_allowed=demo.pets_allowed,
                    seasonality=list(demo.seasonality) or None,
                    typical_crowding=demo.typical_crowding,
                    accessibility={"filters": list(demo.filters)},
                    author_label=user.display_name,
                    freshness_status="unknown",
                    created_at=now,
                    updated_at=now,
                )
                session.add(route)
                await session.flush()
                for position, stop_name in enumerate(demo.stops, start=1):
                    session.add(
                        RouteStop(
                            id=uuid4(),
                            route_id=route.id,
                            place_id=places[stop_name][0],
                            position=position,
                            is_optional=False,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                created += 1
                print(
                    f"  ok: {demo.name} "
                    f"[{demo.difficulty}/{demo.transport_mode}/{len(demo.stops)} стоп.]"
                )
            if apply:
                await session.commit()
    finally:
        await engine.dispose()
    label = "applied" if apply else "dry-run"
    print(f"seed_demo_routes[{label}]: created={created} skipped={skipped}")
    if apply and created:
        print("Next: fill road geometry with scripts/backfill_route_geometry.py")


async def _resolve_author(session: AsyncSession, author: str) -> User:
    user = await session.scalar(
        select(User).where(func.lower(User.display_name) == author.casefold()).limit(1)
    )
    if user is None:
        raise SystemExit(f"author {author!r} not found")
    return user


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", required=True, help="display_name of an existing user")
    parser.add_argument("--apply", action="store_true", help="persist the routes")
    args = parser.parse_args()
    asyncio.run(_run(author=args.author, apply=args.apply))


if __name__ == "__main__":
    main()
