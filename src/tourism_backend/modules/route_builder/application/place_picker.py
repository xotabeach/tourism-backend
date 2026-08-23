"""Deterministic place selection for generated routes (no LLM)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.application.place_covers import covers_for_places
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.route_builder.application.routing import (
    default_max_leg_meters,
    normalize_transport_mode,
)
from tourism_backend.modules.route_builder.application.schemas import (
    DurationOption,
    RouteMatchParamsIn,
)
from tourism_backend.modules.route_builder.application.scoring import (
    INTEREST_KEYWORDS,
    categories_for_interest,
)

_DURATION_STOPS: dict[DurationOption, int] = {
    "d1_2": 3,
    "d3_5": 5,
    "d6_7": 7,
    "d7plus": 9,
}

# Mirrors StubRoutingProvider._ROAD_FACTOR — used to pre-filter candidates so
# the geo-picked chain never trips the routing provider's max-leg check.
_ROAD_FACTOR = 1.35
_LEG_SAFETY_MARGIN = 0.9


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = math.radians(b[1] - a[1])
    dlng = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True, slots=True)
class PickedPlace:
    place_id: UUID
    name: str
    short_description: str | None
    recommended_visit_minutes: int | None
    cover_hint: str | None = None


def _target_stops(duration: DurationOption, max_points: int) -> int:
    return max(2, min(max_points, _DURATION_STOPS[duration]))


def _score_place(
    params: RouteMatchParamsIn,
    place: Place,
    city_cf: str,
    categories: frozenset[str] = frozenset(),
) -> float:
    text = " ".join(
        part
        for part in (
            place.name,
            place.short_description or "",
            place.description or "",
            place.address or "",
            " ".join(place.seasonality or ()),
        )
    ).casefold()
    score = 0.15
    if city_cf and (city_cf in text or city_cf in (place.address or "").casefold()):
        score += 0.35
    for interest in params.interests:
        key = interest.casefold()
        # Taxonomy first: imported places have categories but almost no text.
        if categories & categories_for_interest(key):
            score += 0.12
            continue
        stems = INTEREST_KEYWORDS.get(key, (key,))
        if any(stem in text for stem in stems) or key in text:
            score += 0.12
    if params.season and place.seasonality:
        season = params.season.casefold()
        if any(season in item.casefold() for item in place.seasonality):
            score += 0.1
    if params.with_children is True and place.is_suitable_for_children is True:
        score += 0.08
    if params.with_pets is True and place.is_suitable_for_pets is True:
        score += 0.06
    if params.paid_ok is False and place.is_paid:
        score -= 0.2
    if params.pace == "calm" and (place.difficulty or "").casefold() in {
        "easy",
        "лёгкий",
        "легкий",
        "1",
        "2",
    }:
        score += 0.05
    if place.temporary_closure_status in {"closed", "partial"}:
        score -= 0.5
    return score


async def _categories_for_places(
    session: AsyncSession,
    place_ids: list[UUID],
) -> dict[UUID, frozenset[str]]:
    if not place_ids:
        return {}
    rows = (
        await session.execute(
            select(PlaceCategory.place_id, Category.slug)
            .join(Category, Category.id == PlaceCategory.category_id)
            .where(PlaceCategory.place_id.in_(place_ids))
        )
    ).all()
    grouped: dict[UUID, set[str]] = {}
    for place_id, slug in rows:
        grouped.setdefault(place_id, set()).add(slug)
    return {place_id: frozenset(slugs) for place_id, slugs in grouped.items()}


async def pick_places_for_params(
    session: AsyncSession,
    *,
    params: RouteMatchParamsIn,
    max_points: int,
) -> list[PickedPlace]:
    region = await session.scalar(select(Region).where(Region.slug == params.region_slug))
    if region is None:
        raise AppError(code="region_not_found", message="Регион не найден", status_code=404)

    city_cf = params.city.casefold()
    locality_ids = list(
        await session.scalars(
            select(Locality.id).where(
                Locality.region_id == region.id,
                Locality.status == "active",
                Locality.name.ilike(f"%{params.city}%"),
            )
        )
    )

    stmt = select(Place).where(
        Place.region_id == region.id,
        Place.publication_status == "published",
    )
    if locality_ids:
        stmt = stmt.where(
            or_(
                Place.locality_id.in_(locality_ids),
                Place.name.ilike(f"%{params.city}%"),
                Place.address.ilike(f"%{params.city}%"),
            )
        )
    else:
        stmt = stmt.where(
            or_(
                Place.name.ilike(f"%{params.city}%"),
                Place.address.ilike(f"%{params.city}%"),
                Place.short_description.ilike(f"%{params.city}%"),
            )
        )

    places = list((await session.scalars(stmt.limit(120))).all())
    if len(places) < 2:
        # Fallback: any published places in region.
        places = list(
            (
                await session.scalars(
                    select(Place)
                    .where(
                        Place.region_id == region.id,
                        Place.publication_status == "published",
                    )
                    .order_by(Place.name)
                    .limit(80)
                )
            ).all()
        )

    categories_by_place = await _categories_for_places(session, [place.id for place in places])
    ranked = sorted(
        places,
        key=lambda place: (
            -_score_place(params, place, city_cf, categories_by_place.get(place.id, frozenset())),
            place.name,
        ),
    )
    target = _target_stops(params.duration, max_points)

    # Candidates are ranked by text relevance only, which can span the whole
    # region once the city/locality filter falls back broadly. Chain-select
    # geographically so no consecutive leg exceeds what the (stub or real)
    # RoutingProvider allows for the chosen transport mode — otherwise
    # generate/accept fails with a raw "exceeds max distance" routing error.
    candidate_pool = ranked[: max(target * 6, 30)]
    coords_by_id = await _coords_for_places(session, [place.id for place in candidate_pool])
    geo_candidates = [place for place in candidate_pool if place.id in coords_by_id]

    chosen: list[Place]
    if len(geo_candidates) >= 2:
        mode = normalize_transport_mode(params.transport_mode)
        allowed_m = (default_max_leg_meters(mode) / _ROAD_FACTOR) * _LEG_SAFETY_MARGIN

        remaining = list(geo_candidates)
        chosen = [remaining.pop(0)]
        while len(chosen) < target and remaining:
            last_coords = coords_by_id[chosen[-1].id]
            feasible = [
                place
                for place in remaining
                if _haversine_m(last_coords, coords_by_id[place.id]) <= allowed_m
            ]
            if not feasible:
                break
            next_place = feasible[0]
            chosen.append(next_place)
            remaining.remove(next_place)
    else:
        chosen = ranked[:target]

    if len(chosen) < 2:
        raise AppError(
            code="insufficient_places",
            message="Недостаточно опубликованных мест для генерации маршрута",
            status_code=422,
        )
    covers = await covers_for_places(session, [place.id for place in chosen])
    return [
        PickedPlace(
            place_id=place.id,
            name=place.name,
            short_description=place.short_description,
            recommended_visit_minutes=place.recommended_visit_minutes,
            cover_hint=covers.get(place.id),
        )
        for place in chosen
    ]


async def _coords_for_places(
    session: AsyncSession, place_ids: list[UUID]
) -> dict[UUID, tuple[float, float]]:
    if not place_ids:
        return {}
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom)).where(Place.id.in_(place_ids))
        )
    ).all()
    return {
        place_id: (float(lng), float(lat))
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    }
