"""Deterministic place selection for generated routes (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application.schemas import (
    DurationOption,
    RouteMatchParamsIn,
)
from tourism_backend.modules.route_builder.application.scoring import INTEREST_KEYWORDS

_DURATION_STOPS: dict[DurationOption, int] = {
    "d1_2": 3,
    "d3_5": 5,
    "d6_7": 7,
    "d7plus": 9,
}


@dataclass(frozen=True, slots=True)
class PickedPlace:
    place_id: UUID
    name: str
    short_description: str | None
    recommended_visit_minutes: int | None
    cover_hint: str | None = None


def _target_stops(duration: DurationOption, max_points: int) -> int:
    return max(2, min(max_points, _DURATION_STOPS[duration]))


def _score_place(params: RouteMatchParamsIn, place: Place, city_cf: str) -> float:
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

    ranked = sorted(
        places,
        key=lambda place: (-_score_place(params, place, city_cf), place.name),
    )
    target = _target_stops(params.duration, max_points)
    chosen = ranked[:target]
    if len(chosen) < 2:
        raise AppError(
            code="insufficient_places",
            message="Недостаточно опубликованных мест для генерации маршрута",
            status_code=422,
        )
    return [
        PickedPlace(
            place_id=place.id,
            name=place.name,
            short_description=place.short_description,
            recommended_visit_minutes=place.recommended_visit_minutes,
        )
        for place in chosen
    ]
