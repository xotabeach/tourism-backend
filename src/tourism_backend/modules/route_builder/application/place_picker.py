"""Deterministic place selection for generated routes (no LLM)."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import cast as type_cast
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.api.errors import AppError
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.application.osm_field_promotion import safety_tags_from_payload
from tourism_backend.modules.places.application.place_covers import covers_for_places
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.route_builder.application.discovery import area_bounds
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
    UserPreferenceSignals,
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
_START_RADIUS_METERS = {
    "walk": 12_000,
    "car": 40_000,
    "public": 25_000,
    "mixed": 40_000,
}


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
    difficulty: str | None = None
    suitable_for_children: bool | None = None
    suitable_for_pets: bool | None = None
    temporary_closure_status: str | None = None
    safety_warnings: tuple[str, ...] = ()
    seasonality: tuple[str, ...] = ()
    surface: str | None = None
    accessibility: dict[str, object] | None = None
    osm_tags: dict[str, str] | None = None
    access_transport: tuple[str, ...] = ()
    payment_status: str = "unknown"
    is_paid: bool = False
    price_min_amount: int | None = None
    price_currency: str = "RUB"
    typical_crowding: str = "unknown"
    locality_name: str | None = None


def _target_stops(duration: DurationOption, max_points: int) -> int:
    return max(2, min(max_points, _DURATION_STOPS[duration]))


def _score_place(
    params: RouteMatchParamsIn,
    place: Place,
    location_cf: str,
    categories: frozenset[str] = frozenset(),
    preferences: UserPreferenceSignals | None = None,
    recent_place_ids: frozenset[UUID] = frozenset(),
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
    if location_cf and (location_cf in text or location_cf in (place.address or "").casefold()):
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
    if params.avoid_crowds:
        score += {"low": 0.1, "medium": -0.04, "high": -0.15}.get(place.typical_crowding, 0)
    if params.budget_amount is not None:
        # Compare only known rouble prices; unknown is not the same as free.
        if place.payment_status == "free" and not place.is_paid and not place.price_min_amount:
            score += 0.1
        elif place.price_min_amount is not None and place.price_currency == "RUB":
            headroom = 1 - place.price_min_amount / max(1, params.budget_amount)
            score += 0.1 * max(-1, min(1, headroom))
    # Variety stays weaker than an explicit interest/category match. A small
    # catalogue can still reuse places instead of failing an otherwise valid trip.
    if place.id in recent_place_ids:
        score -= 0.1
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
    if preferences is not None:
        preferred_categories: set[str] = set()
        for category in preferences.categories:
            preferred_categories.update(categories_for_interest(category))
        if preferred_categories and preferred_categories & set(categories):
            # A small bonus keeps profile preferences useful for generation
            # without turning them into a hard filter.
            score += 0.08
        if preferences.difficulty and place.difficulty:
            score += 0.05 if place.difficulty.casefold() == preferences.difficulty.casefold() else 0
        if preferences.travels_with_kids and place.is_suitable_for_children is True:
            score += 0.05
        if preferences.travels_with_pets and place.is_suitable_for_pets is True:
            score += 0.05
    return score


def picked_place_from_orm(place: Place, *, cover_hint: str | None = None) -> PickedPlace:
    """Copy the safety fields the quality gate can evaluate without ORM types."""

    return PickedPlace(
        place_id=place.id,
        name=place.name,
        short_description=place.short_description,
        recommended_visit_minutes=place.recommended_visit_minutes,
        cover_hint=cover_hint,
        difficulty=place.difficulty,
        suitable_for_children=place.is_suitable_for_children,
        suitable_for_pets=place.is_suitable_for_pets,
        temporary_closure_status=place.temporary_closure_status,
        safety_warnings=tuple((place.safety_warnings or [])[:16]),
        seasonality=tuple((place.seasonality or [])[:16]),
        surface=place.surface,
        accessibility=(
            dict(place.accessibility) if isinstance(place.accessibility, dict) else None
        ),
        osm_tags=safety_tags_from_payload(place.source_payload),
        access_transport=tuple((place.access_transport or [])[:16]),
        payment_status=place.payment_status or "unknown",
        is_paid=bool(place.is_paid),
        price_min_amount=place.price_min_amount,
        price_currency=place.price_currency or "RUB",
        typical_crowding=place.typical_crowding or "unknown",
    )


def place_planning_warnings(params: RouteMatchParamsIn, places: list[PickedPlace]) -> list[str]:
    warnings: list[str] = []
    if params.budget_amount is not None or params.paid_ok is False:
        unknown_prices = sum(
            1
            for place in places
            if not (
                (
                    place.payment_status == "free"
                    and not place.is_paid
                    and not place.price_min_amount
                )
                or (place.price_min_amount is not None and place.price_currency == "RUB")
            )
        )
        if unknown_prices:
            warnings.append(
                f"Для {unknown_prices} из {len(places)} мест стоимость посещения в рублях "
                "не подтверждена. Неизвестная цена не означает бесплатный вход."
            )
        warnings.append(
            "Бюджет учтён при выборе мест, но это ещё не смета поездки: "
            "нужно уточнить тарифы на компанию, питание, транспорт и ночлег."
        )
    if params.avoid_crowds:
        if any(place.typical_crowding in {"unknown", ""} for place in places):
            warnings.append(
                "Для части мест нет данных о людности; отсутствие очередей не подтверждено."
            )
        if any(place.typical_crowding == "high" for place in places):
            warnings.append(
                "В план вошли и обычно людные места. Можно заменить их или выбрать другое время."
            )
    return warnings


def _hard_place_constraints(params: RouteMatchParamsIn) -> tuple[ColumnElement[bool], ...]:
    """Return constraints that should prevent an avoidable bad candidate."""

    constraints: list[ColumnElement[bool]] = [
        or_(
            Place.temporary_closure_status.is_(None),
            ~Place.temporary_closure_status.in_(
                ("closed", "temporarily_closed", "closed_permanently")
            ),
        )
    ]
    if params.with_children is True:
        constraints.append(
            or_(
                Place.is_suitable_for_children.is_(True),
                Place.is_suitable_for_children.is_(None),
            )
        )
    if params.with_pets is True:
        constraints.append(
            or_(
                Place.is_suitable_for_pets.is_(True),
                Place.is_suitable_for_pets.is_(None),
            )
        )
    if params.paid_ok is False or params.budget_amount == 0:
        constraints.append(
            and_(
                Place.is_paid.is_(False),
                Place.payment_status != "paid",
                or_(Place.price_min_amount.is_(None), Place.price_min_amount <= 0),
            )
        )
    elif params.budget_amount is not None:
        # One visit whose known minimum exceeds a whole day's budget cannot
        # fit. Do not compare currencies without a verified exchange rate.
        constraints.append(
            or_(
                Place.price_min_amount.is_(None),
                Place.price_currency != "RUB",
                Place.price_min_amount <= params.budget_amount,
            )
        )
    return tuple(constraints)


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
    preferences: UserPreferenceSignals | None = None,
    recent_place_ids: frozenset[UUID] = frozenset(),
) -> list[PickedPlace]:
    region = await session.scalar(select(Region).where(Region.slug == params.region_slug))
    if region is None:
        raise AppError(code="region_not_found", message="Регион не найден", status_code=404)

    location_query = params.effective_start_query
    location_cf = (location_query or "").casefold()
    # ``city`` is the legacy locality field. Do not probe it as a POI first:
    # besides wasting a query, a mocked/session-default scalar can be mistaken
    # for a place. A new ``start_query`` may intentionally name either kind.
    start_place = await _resolve_anchor_place(
        session,
        region_id=region.id,
        place_id=params.start_place_id,
        query=params.start_query,
    )
    finish_place = await _resolve_anchor_place(
        session,
        region_id=region.id,
        place_id=params.finish_place_id,
        query=params.finish_query,
    )
    locality_ids = await _resolve_locality_ids(
        session,
        region_id=region.id,
        explicit_id=params.start_locality_id,
        query=location_query if start_place is None else None,
        preferred_names=params.preferred_localities if not location_query else (),
    )

    base_stmt = select(Place).where(
        Place.region_id == region.id,
        Place.publication_status == "published",
        *_hard_place_constraints(params),
    )
    stmt = base_stmt
    if locality_ids:
        stmt = stmt.where(Place.locality_id.in_(locality_ids))
    elif start_place is not None:
        # A catalogue point already knows its settlement. Prefer that coherent
        # local cluster over a wide radius that can jump across mountains (for
        # example, from seaside Simeiz straight to Ai-Petri on a walking trip).
        if start_place.locality_id is not None:
            stmt = stmt.where(Place.locality_id == start_place.locality_id)
        else:
            radius = _START_RADIUS_METERS[normalize_transport_mode(params.transport_mode)]
            stmt = stmt.where(func.ST_DWithin(Place.location, start_place.location, radius))
    elif location_query and location_query.casefold() not in {"крым", "crimea", "весь крым"}:
        stmt = stmt.where(
            or_(
                Place.name.ilike(f"%{location_query}%"),
                Place.address.ilike(f"%{location_query}%"),
                Place.short_description.ilike(f"%{location_query}%"),
            )
        )
    elif params.search_area and (bounds := area_bounds(params.search_area)):
        geom = cast(Place.location, Geometry)
        stmt = stmt.where(
            ST_X(geom).between(bounds[0], bounds[2]),
            ST_Y(geom).between(bounds[1], bounds[3]),
        )

    places = list((await session.scalars(stmt.limit(120))).all())
    if len(places) < 2 and locality_ids:
        # The curated catalogue can know a settlement before enough places
        # have been assigned to it. Keep the user's recognised locality as
        # the anchor and widen only to a transport-appropriate nearby radius;
        # this is intentionally different from the region-wide fallback used
        # for an explicitly flexible start.
        locality_center = (
            select(Locality.center)
            .where(Locality.id == locality_ids[0], Locality.center.is_not(None))
            .scalar_subquery()
        )
        radius = _START_RADIUS_METERS[normalize_transport_mode(params.transport_mode)]
        places = list(
            (
                await session.scalars(
                    base_stmt.where(func.ST_DWithin(Place.location, locality_center, radius)).limit(
                        120
                    )
                )
            ).all()
        )
    if len(places) < 2 and start_place is not None:
        # Sparse settlement data may require a nearby fallback, but keep it
        # transport-aware and anchored to the chosen point.
        radius = _START_RADIUS_METERS[normalize_transport_mode(params.transport_mode)]
        places = list(
            (
                await session.scalars(
                    base_stmt.where(
                        func.ST_DWithin(Place.location, start_place.location, radius)
                    ).limit(120)
                )
            ).all()
        )
    if len(places) < 2:
        # Unknown free text may broaden only when the user delegated the
        # choice. An exact request must fail visibly instead of silently
        # building a route on the other side of Crimea.
        if location_query and not params.flexible_start:
            raise AppError(
                code="location_not_found",
                message=(
                    "Не нашли опубликованные точки рядом с указанным местом. "
                    "Уточните название или разрешите подобрать старт автоматически."
                ),
                status_code=422,
            )
        places = list(
            (
                await session.scalars(
                    select(Place)
                    .where(
                        Place.region_id == region.id,
                        Place.publication_status == "published",
                        *_hard_place_constraints(params),
                    )
                    .order_by(Place.name)
                    .limit(80)
                )
            ).all()
        )

    for anchor in (start_place, finish_place):
        if anchor is not None and all(place.id != anchor.id for place in places):
            places.append(anchor)

    categories_by_place = await _categories_for_places(session, [place.id for place in places])
    ranked = sorted(
        places,
        key=lambda place: (
            -_score_place(
                params,
                place,
                location_cf,
                categories_by_place.get(place.id, frozenset()),
                preferences,
                recent_place_ids,
            ),
            place.name,
        ),
    )
    target = _target_stops(params.duration, max_points)

    # Ranked candidates can span the whole
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
        if start_place is not None and start_place in remaining:
            remaining.remove(start_place)
            chosen = [start_place]
        else:
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

    if finish_place is not None:
        chosen = [place for place in chosen if place.id != finish_place.id]
        chosen = chosen[: max(1, target - 1)]
        chosen.append(finish_place)

    if len(chosen) < 2:
        raise AppError(
            code="insufficient_places",
            message="Недостаточно опубликованных мест для генерации маршрута",
            status_code=422,
        )
    covers = await covers_for_places(session, [place.id for place in chosen])
    locality_names = await _locality_names_for_places(session, chosen)
    return [
        replace(
            picked_place_from_orm(place, cover_hint=covers.get(place.id)),
            locality_name=locality_names.get(place.id),
        )
        for place in chosen
    ]


async def _resolve_anchor_place(
    session: AsyncSession,
    *,
    region_id: UUID,
    place_id: str | None,
    query: str | None,
) -> Place | None:
    if place_id:
        candidate = await session.get(Place, UUID(place_id))
        if (
            candidate is not None
            and candidate.region_id == region_id
            and candidate.publication_status == "published"
            and candidate.merged_into_place_id is None
        ):
            return candidate
    if not query or query.casefold() in {"крым", "crimea", "весь крым"}:
        return None
    return type_cast(
        Place | None,
        await session.scalar(
            select(Place)
            .where(
                Place.region_id == region_id,
                Place.publication_status == "published",
                Place.merged_into_place_id.is_(None),
                func.lower(Place.name) == query.casefold(),
            )
            .order_by(Place.name)
            .limit(1)
        ),
    )


async def _resolve_locality_ids(
    session: AsyncSession,
    *,
    region_id: UUID,
    explicit_id: str | None,
    query: str | None,
    preferred_names: list[str] | tuple[str, ...],
) -> list[UUID]:
    if explicit_id:
        locality = await session.get(Locality, UUID(explicit_id))
        if locality is not None and locality.region_id == region_id and locality.status == "active":
            return [locality.id]
    names = [name for name in [query, *preferred_names] if name]
    if not names:
        return []
    clauses = [func.lower(Locality.name) == name.casefold() for name in names]
    return list(
        await session.scalars(
            select(Locality.id)
            .where(
                Locality.region_id == region_id,
                Locality.status == "active",
                or_(*clauses),
            )
            .limit(8)
        )
    )


async def _locality_names_for_places(
    session: AsyncSession,
    places: list[Place],
) -> dict[UUID, str]:
    locality_ids = {place.locality_id for place in places if place.locality_id is not None}
    if not locality_ids:
        return {}
    names = {
        locality.id: locality.name
        for locality in (
            await session.scalars(select(Locality).where(Locality.id.in_(locality_ids)))
        ).all()
    }
    return {
        place.id: names[place.locality_id]
        for place in places
        if place.locality_id is not None and place.locality_id in names
    }


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
