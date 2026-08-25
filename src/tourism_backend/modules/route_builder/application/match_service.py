"""Catalog match: load public routes, score, return ranked bands."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
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
    RouteMatchCandidate,
    partition_scored,
    score_candidate,
)
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteListItemOut
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop
from tourism_backend.modules.subscriptions.application import service as travel_plus
from tourism_backend.modules.subscriptions.application.entitlements import policy_for_user


async def match_routes(
    session: AsyncSession,
    *,
    user_id: UUID,
    params: RouteMatchParamsIn,
    ai_planning_enabled: bool = False,
) -> RouteMatchOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    await travel_plus.refresh_user_travel_plus(session, user=user)
    policy = policy_for_user(user)

    candidates = await _load_candidates(session, region_slug=params.region_slug)
    scored = [score_candidate(params, candidate) for candidate in candidates]
    ideal_scored, close_scored, offer_generate = partition_scored(scored)

    route_ids = [item.candidate.route_id for item in ideal_scored + close_scored]
    items_by_id = await _list_items_by_ids(session, route_ids)

    ideal = [
        RouteMatchHitOut(
            route=items_by_id[item.candidate.route_id],
            score=item.score,
            band="ideal",
            reasons=list(item.reasons),
        )
        for item in ideal_scored
        if item.candidate.route_id in items_by_id
    ]
    close = [
        RouteMatchHitOut(
            route=items_by_id[item.candidate.route_id],
            score=item.score,
            band="close",
            reasons=list(item.reasons),
        )
        for item in close_scored
        if item.candidate.route_id in items_by_id
    ]

    ai_rerank_eligible = bool(ai_planning_enabled and policy.ai_chat_enabled)
    snap = await quota_snapshot(session, user_id=user_id, policy=policy)

    return RouteMatchOut(
        strategy="algorithmic",
        ideal=ideal,
        close=close,
        offer_generate=offer_generate,
        ai_rerank_eligible=ai_rerank_eligible,
        ai_rerank_applied=False,
        scored_total=len(candidates),
        params_echo=params,
        quota=snap,
    )


async def _load_candidates(
    session: AsyncSession,
    *,
    region_slug: str,
) -> list[RouteMatchCandidate]:
    routes = list(
        (
            await session.scalars(
                select(Route)
                .join(Region, Region.id == Route.region_id)
                .where(
                    *routes_service._PUBLIC_CATALOG,  # noqa: SLF001
                    ~routes_service._has_unpublished_stop(),  # noqa: SLF001
                    Region.slug == region_slug,
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
            )
            .join(Place, Place.id == RouteStop.place_id)
            .outerjoin(Locality, Locality.id == Place.locality_id)
            .where(RouteStop.route_id.in_(route_ids))
            .order_by(RouteStop.route_id, RouteStop.position)
        )
    ).all()

    places_by_route: dict[UUID, list[str]] = defaultdict(list)
    localities_by_route: dict[UUID, list[str]] = defaultdict(list)
    for route_id, place_name, locality_name in stop_rows:
        places_by_route[route_id].append(place_name)
        if locality_name:
            localities_by_route[route_id].append(locality_name)

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
                stops_count=counts.get(route.id, 0),
                category_slugs=frozenset(categories_by_route.get(route.id, frozenset())),
            )
        )
    return out


async def _list_items_by_ids(
    session: AsyncSession,
    route_ids: list[UUID],
) -> dict[UUID, RouteListItemOut]:
    if not route_ids:
        return {}
    routes = list((await session.scalars(select(Route).where(Route.id.in_(route_ids)))).all())
    by_id = {route.id: route for route in routes}
    ordered = [by_id[route_id] for route_id in route_ids if route_id in by_id]
    counts = await routes_service._stops_count_map(session, route_ids)  # noqa: SLF001
    covers = await routes_service._cover_urls_for_routes(session, route_ids)  # noqa: SLF001
    authors = await routes_service._author_fields_for_routes(session, ordered)  # noqa: SLF001
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
        )
    return items
