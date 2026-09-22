"""Catalog match: load public routes, score, return ranked bands."""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.route_builder.application.quota import quota_snapshot
from tourism_backend.modules.route_builder.application.schemas import (
    RouteMatchHitOut,
    RouteMatchOut,
    RouteMatchParamsIn,
)
from tourism_backend.modules.route_builder.application.scoring import (
    MATCH_FORMULA_VERSION,
    MIN_CHAT_SIGNALS,
    RouteMatchCandidate,
    ScoredMatch,
    UserPreferenceSignals,
    band_of,
    legacy_bands,
    match_percent,
    requested_signal_count,
    score_candidate,
    select_hits,
)
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteListItemOut
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop
from tourism_backend.modules.subscriptions.application import service as travel_plus
from tourism_backend.modules.subscriptions.application.entitlements import policy_for_user

logger = logging.getLogger(__name__)


async def match_routes(
    session: AsyncSession,
    *,
    user_id: UUID,
    params: RouteMatchParamsIn,
    ai_planning_enabled: bool = False,
    confirmed_fields: list[str] | None = None,
    excluded_route_ids: frozenset[UUID] = frozenset(),
) -> RouteMatchOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    await travel_plus.refresh_user_travel_plus(session, user=user)
    policy = policy_for_user(user)

    candidates = await _load_candidates(
        session,
        region_slug=params.region_slug,
        excluded_route_ids=excluded_route_ids,
    )
    preferences = UserPreferenceSignals(
        categories=frozenset(user.preferred_categories or ()),
        difficulty=user.preferred_difficulty,
        travels_with_kids=user.travels_with_kids,
        travels_with_pets=user.travels_with_pets,
    )
    if confirmed_fields is not None:
        # Chat discovery must not silently apply unconfirmed form defaults.
        optional = {
            "transport_mode",
            "trip_type",
            "season",
            "budget_amount",
            "paid_ok",
            "with_children",
            "with_pets",
            "avoid_crowds",
        }
        params = params.model_copy(
            update={
                **{key: None for key in optional if key not in confirmed_fields},
                **({"interests": []} if "interests" not in confirmed_fields else {}),
            }
        )
    scored = [
        score_candidate(
            params,
            candidate,
            preferences,
            confirmed_fields=confirmed_fields,
            explicit_fields=params.explicit_fields,
        )
        for candidate in candidates
    ]
    signals = requested_signal_count(
        params, confirmed_fields=confirmed_fields, explicit_fields=params.explicit_fields
    )
    # The chat only promises a percent once enough was confirmed (D7).
    show_percent = confirmed_fields is None or signals >= MIN_CHAT_SIGNALS

    # Equal scores: better rated first, then by name (D18).
    rating_by_route = await _ratings_for(session, [item.candidate.route_id for item in scored])

    def tiebreak(item: ScoredMatch) -> tuple[float, str]:
        return rating_by_route.get(item.candidate.route_id, 0.0), item.candidate.name

    hit_scored, offer_generate = select_hits(scored, tiebreak=tiebreak)
    legacy_ideal, legacy_close = legacy_bands(hit_scored)

    route_ids = [item.candidate.route_id for item in hit_scored]
    items_by_id = await _list_items_by_ids(session, route_ids)

    def to_hit(item: ScoredMatch) -> RouteMatchHitOut:
        return RouteMatchHitOut(
            route=items_by_id[item.candidate.route_id],
            score=item.score,
            band=band_of(item),  # type: ignore[arg-type]
            reasons=list(item.reasons),
            locality_label=_locality_label(item.candidate.locality_names),
            match_percent=match_percent(item) if show_percent else None,
            mismatches=list(item.mismatches),
            partial_data=item.partial_data,
        )

    hits = [to_hit(item) for item in hit_scored if item.candidate.route_id in items_by_id]
    ideal = [to_hit(item) for item in legacy_ideal if item.candidate.route_id in items_by_id]
    close = [to_hit(item) for item in legacy_close if item.candidate.route_id in items_by_id]
    _log_outcome(
        hit_scored, signals=signals, empty=not hits, confirmed=confirmed_fields is not None
    )

    ai_rerank_eligible = bool(ai_planning_enabled and policy.ai_chat_enabled)
    snap = await quota_snapshot(session, user_id=user_id, policy=policy)

    return RouteMatchOut(
        strategy="algorithmic",
        ideal=ideal,
        close=close,
        hits=hits,
        requested_signals=signals,
        formula_version=MATCH_FORMULA_VERSION,
        offer_generate=offer_generate,
        ai_rerank_eligible=ai_rerank_eligible,
        ai_rerank_applied=False,
        scored_total=len(candidates),
        params_echo=params,
        quota=snap,
    )


def _log_outcome(hits: list[ScoredMatch], *, signals: int, empty: bool, confirmed: bool) -> None:
    """One line per match: enough to see the spread of percents and empty results."""

    best = hits[0] if hits else None
    logger.info(
        "route_match formula=%d channel=%s signals=%d hits=%d best=%s partial=%s empty=%s",
        MATCH_FORMULA_VERSION,
        "chat" if confirmed else "form",
        signals,
        len(hits),
        f"{best.score:.2f}" if best else "-",
        best.partial_data if best else "-",
        empty,
    )


async def _ratings_for(session: AsyncSession, route_ids: list[UUID]) -> dict[UUID, float]:
    if not route_ids:
        return {}
    ratings = await routes_service.route_ratings(session, route_ids)
    return {route_id: float(average) for route_id, (average, _count) in ratings.items() if average}


def _locality_label(names: tuple[str, ...]) -> str | None:
    unique = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not unique:
        return None
    if len(unique) <= 2:
        return " · ".join(unique)[:120]
    return f"{unique[0]} · {unique[1]} и ещё {len(unique) - 2}"[:120]


async def _load_candidates(
    session: AsyncSession,
    *,
    region_slug: str,
    excluded_route_ids: frozenset[UUID] = frozenset(),
) -> list[RouteMatchCandidate]:
    exclusions = (Route.id.notin_(excluded_route_ids),) if excluded_route_ids else ()
    routes = list(
        (
            await session.scalars(
                select(Route)
                .join(Region, Region.id == Route.region_id)
                .where(
                    *routes_service._PUBLIC_CATALOG,  # noqa: SLF001
                    ~routes_service._has_unpublished_stop(),  # noqa: SLF001
                    Region.slug == region_slug,
                    *exclusions,
                )
                .order_by(Route.name, Route.id)
                .limit(200)
            )
        ).all()
    )
    if not routes:
        return []

    route_ids = [route.id for route in routes]
    stop_rows = (
        await session.execute(
            select(
                RouteStop.route_id,
                Place.name,
                Locality.name,
                ST_X(cast(Place.location, Geometry)),
                ST_Y(cast(Place.location, Geometry)),
            )
            .join(Place, Place.id == RouteStop.place_id)
            .outerjoin(Locality, Locality.id == Place.locality_id)
            .where(RouteStop.route_id.in_(route_ids))
            .order_by(RouteStop.route_id, RouteStop.position)
        )
    ).all()

    places_by_route: dict[UUID, list[str]] = defaultdict(list)
    localities_by_route: dict[UUID, list[str]] = defaultdict(list)
    coordinates_by_route: dict[UUID, list[tuple[float, float]]] = defaultdict(list)
    for route_id, place_name, locality_name, lng, lat in stop_rows:
        places_by_route[route_id].append(place_name)
        if locality_name:
            localities_by_route[route_id].append(locality_name)
        if lng is not None and lat is not None:
            coordinates_by_route[route_id].append((float(lng), float(lat)))

    # Distinct category slugs across each route's stops (ADR-009).
    category_rows = (
        await session.execute(
            select(RouteStop.route_id, Category.slug)
            .join(PlaceCategory, PlaceCategory.place_id == RouteStop.place_id)
            .join(Category, Category.id == PlaceCategory.category_id)
            .where(RouteStop.route_id.in_(route_ids))
        )
    ).all()
    categories_by_route: dict[UUID, set[str]] = defaultdict(set)
    for route_id, slug in category_rows:
        categories_by_route[route_id].add(slug)

    counts = await routes_service._stops_count_map(session, route_ids)  # noqa: SLF001
    out: list[RouteMatchCandidate] = []
    for route in routes:
        seasonality = tuple(route.seasonality or [])
        out.append(
            RouteMatchCandidate(
                route_id=route.id,
                name=route.name,
                short_description=route.short_description,
                description=route.description,
                estimated_duration_minutes=route.estimated_duration_minutes,
                difficulty=route.difficulty,
                transport_mode=route.transport_mode,
                seasonality=seasonality,
                suitable_for_children=route.suitable_for_children,
                pets_allowed=route.pets_allowed,
                place_names=tuple(places_by_route.get(route.id, ())),
                locality_names=tuple(dict.fromkeys(localities_by_route.get(route.id, ()))),
                stop_coordinates=tuple(coordinates_by_route.get(route.id, ())),
                stops_count=counts.get(route.id, 0),
                category_slugs=frozenset(categories_by_route.get(route.id, frozenset())),
                typical_crowding=route.typical_crowding,
                price_min_amount=route.price_min_amount,
                is_seaside=route.is_seaside,
            )
        )
    return out


async def _list_items_by_ids(
    session: AsyncSession,
    route_ids: list[UUID],
) -> dict[UUID, RouteListItemOut]:
    if not route_ids:
        return {}
    routes = list(
        (
            await session.scalars(
                select(Route).where(
                    Route.id.in_(route_ids),
                    *routes_service._PUBLIC_CATALOG,
                    ~routes_service._has_unpublished_stop(),
                )
            )
        ).all()
    )
    by_id = {route.id: route for route in routes}
    ordered = [by_id[route_id] for route_id in route_ids if route_id in by_id]
    counts = await routes_service._stops_count_map(session, route_ids)  # noqa: SLF001
    covers = await routes_service._cover_urls_for_routes(session, route_ids)  # noqa: SLF001
    authors = await routes_service._author_fields_for_routes(session, ordered)  # noqa: SLF001
    ratings = await routes_service.route_ratings(session, route_ids)
    items: dict[UUID, RouteListItemOut] = {}
    for route in ordered:
        owner_id, label, avatar, is_expert, rank_title = authors[route.id]
        items[route.id] = routes_service._to_list_item(  # noqa: SLF001
            route,
            counts.get(route.id, 0),
            covers.get(route.id),
            owner_user_id=owner_id,
            author_label=label,
            author_avatar_url=avatar,
            author_is_expert=is_expert,
            author_rank_title=rank_title,
            rating=ratings.get(route.id, (None, 0)),
        )
    return items


async def public_catalogue_routes(
    session: AsyncSession, route_ids: list[UUID]
) -> list[RouteListItemOut]:
    return list((await _list_items_by_ids(session, list(dict.fromkeys(route_ids))[:5])).values())
