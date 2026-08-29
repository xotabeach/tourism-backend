"""Load catalog candidates, persist a daily deck, and record skips."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from tourism_backend.api.errors import AppError
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Category, Place, PlaceCategory
from tourism_backend.modules.recommendations.application.policy import (
    CANDIDATE_LIMIT,
    DECK_SIZE,
    MAX_USERS_PER_RUN,
    RANKER_VERSION,
    deck_date_for,
    season_for,
)
from tourism_backend.modules.recommendations.application.ranker import (
    RecommendationCandidate,
    RecommendationProfile,
    ScoredRecommendation,
    completed_cutoff,
    cooldown_cutoff,
    rerank_for_diversity,
    score_candidates,
)
from tourism_backend.modules.recommendations.application.schemas import (
    ExplanationCode,
    RecommendationCardOut,
    RecommendationDeckOut,
    RecommendationFeedbackIn,
    RecommendationFeedbackOut,
)
from tourism_backend.modules.recommendations.infrastructure.models import (
    RouteRecommendationDeckItem,
    RouteRecommendationFeedback,
)
from tourism_backend.modules.route_execution.infrastructure.models import RouteExecution
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

_PUBLIC_CATALOG = (
    or_(Route.source == "editorial", Route.source == "user_created"),
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)
_EXPLANATION_CODES = frozenset(
    {
        "matches_interest",
        "nearby_exploration",
        "fresh_route",
        "popular_route",
        "cold_start",
    }
)


def _has_unpublished_stop() -> Exists:
    return exists().where(
        RouteStop.route_id == Route.id,
        RouteStop.place_id == Place.id,
        Place.publication_status != "published",
    )


def _quality_status(accessibility: object) -> str:
    if not isinstance(accessibility, dict):
        return "unknown"
    routing = accessibility.get("routing")
    if not isinstance(routing, dict):
        return "unknown"
    status = routing.get("quality_status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return "unknown"


def _explanation_code(value: str) -> ExplanationCode:
    if value in _EXPLANATION_CODES:
        return cast(ExplanationCode, value)
    return "cold_start"


def _feedback_action(value: str) -> Literal["skip"]:
    return "skip"


async def get_today_deck(
    session: AsyncSession,
    *,
    user_id: UUID,
    as_of: datetime | None = None,
) -> RecommendationDeckOut:
    now = as_of or datetime.now(UTC)
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    deck_date = deck_date_for(now)
    existing = await _load_deck_rows(session, user_id=user_id, deck_date=deck_date)
    generated = False
    if not existing:
        await _persist_deck(session, user=user, as_of=now)
        generated = True
        existing = await _load_deck_rows(session, user_id=user_id, deck_date=deck_date)
    profile = await _load_profile(session, user=user, as_of=now)
    visible = [
        row
        for row in existing
        if row.route_id not in profile.favorite_route_ids
        and row.route_id not in profile.skipped_route_ids
        and row.route_id not in profile.completed_route_ids
    ]
    cards = await _hydrate_cards(session, visible)
    return RecommendationDeckOut(
        deck_date=deck_date,
        ranker_version=RANKER_VERSION,
        generated=generated,
        items=cards,
        remaining=len(cards),
    )


async def record_feedback(
    session: AsyncSession,
    *,
    user_id: UUID,
    route_id: UUID,
    payload: RecommendationFeedbackIn,
    as_of: datetime | None = None,
) -> RecommendationFeedbackOut:
    now = as_of or datetime.now(UTC)
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    replayed = await _feedback_by_event(
        session,
        user_id=user_id,
        client_event_id=payload.client_event_id,
    )
    if replayed is not None:
        return _feedback_out(replayed, replayed=True)

    public_route = await session.scalar(
        select(Route.id).where(Route.id == route_id, *_PUBLIC_CATALOG, ~_has_unpublished_stop())
    )
    if public_route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)

    event = RouteRecommendationFeedback(
        id=uuid4(),
        user_id=user_id,
        route_id=route_id,
        action=payload.action,
        deck_date=payload.deck_date or deck_date_for(now),
        ranker_version=RANKER_VERSION,
        client_event_id=payload.client_event_id,
        created_at=now,
    )
    session.add(event)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _feedback_by_event(
            session,
            user_id=user_id,
            client_event_id=payload.client_event_id,
        )
        if existing is None:
            raise
        return _feedback_out(existing, replayed=True)
    return _feedback_out(event, replayed=False)


async def generate_missing_decks(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    as_of: datetime | None = None,
    persist: bool = False,
) -> dict[str, int]:
    """Fill today's deck for a bounded page of users. Dry-run unless persist."""

    now = as_of or datetime.now(UTC)
    if not 1 <= limit <= MAX_USERS_PER_RUN:
        raise AppError(
            code="validation_error",
            message=f"limit must be between 1 and {MAX_USERS_PER_RUN}",
            status_code=422,
        )
    if offset < 0:
        raise AppError(code="validation_error", message="offset must be >= 0", status_code=422)
    deck_date = deck_date_for(now)
    user_ids = list(
        (
            await session.scalars(
                select(User.id).order_by(User.created_at, User.id).offset(offset).limit(limit)
            )
        ).all()
    )
    if not user_ids:
        return {
            "scanned": 0,
            "already_present": 0,
            "missing": 0,
            "generated": 0,
        }
    existing = set(
        (
            await session.scalars(
                select(RouteRecommendationDeckItem.user_id)
                .where(
                    RouteRecommendationDeckItem.deck_date == deck_date,
                    RouteRecommendationDeckItem.ranker_version == RANKER_VERSION,
                    RouteRecommendationDeckItem.user_id.in_(user_ids),
                )
                .distinct()
            )
        ).all()
    )
    missing = [user_id for user_id in user_ids if user_id not in existing]
    generated = 0
    if persist:
        for user_id in missing:
            user = await session.get(User, user_id)
            if user is None:
                continue
            await _persist_deck(session, user=user, as_of=now)
            generated += 1
    return {
        "scanned": len(user_ids),
        "already_present": len(user_ids) - len(missing),
        "missing": len(missing),
        "generated": generated,
    }


def _feedback_out(
    event: RouteRecommendationFeedback,
    *,
    replayed: bool,
) -> RecommendationFeedbackOut:
    return RecommendationFeedbackOut(
        route_id=event.route_id,
        action=_feedback_action(event.action),
        client_event_id=event.client_event_id,
        deck_date=event.deck_date,
        ranker_version=event.ranker_version,
        created_at=event.created_at,
        replayed=replayed,
    )


async def _feedback_by_event(
    session: AsyncSession,
    *,
    user_id: UUID,
    client_event_id: UUID,
) -> RouteRecommendationFeedback | None:
    event: RouteRecommendationFeedback | None = await session.scalar(
        select(RouteRecommendationFeedback).where(
            RouteRecommendationFeedback.user_id == user_id,
            RouteRecommendationFeedback.client_event_id == client_event_id,
        )
    )
    return event


async def _load_deck_rows(
    session: AsyncSession,
    *,
    user_id: UUID,
    deck_date: date,
) -> list[RouteRecommendationDeckItem]:
    return list(
        (
            await session.scalars(
                select(RouteRecommendationDeckItem)
                .where(
                    RouteRecommendationDeckItem.user_id == user_id,
                    RouteRecommendationDeckItem.deck_date == deck_date,
                    RouteRecommendationDeckItem.ranker_version == RANKER_VERSION,
                )
                .order_by(RouteRecommendationDeckItem.rank, RouteRecommendationDeckItem.route_id)
            )
        ).all()
    )


async def _hydrate_cards(
    session: AsyncSession,
    rows: list[RouteRecommendationDeckItem],
) -> list[RecommendationCardOut]:
    items = await routes_service.list_catalog_items_by_ids(
        session,
        [row.route_id for row in rows],
    )
    cards: list[RecommendationCardOut] = []
    for row in rows:
        route = items.get(row.route_id)
        if route is None:
            continue
        cards.append(
            RecommendationCardOut(
                route=route,
                rank=int(row.rank),
                score=float(row.score),
                explanation_code=_explanation_code(row.explanation_code),
                exploration=bool(row.exploration),
            )
        )
    return cards


async def _persist_deck(session: AsyncSession, *, user: User, as_of: datetime) -> None:
    deck_date = deck_date_for(as_of)
    profile = await _load_profile(session, user=user, as_of=as_of)
    candidates = await _load_candidates(session)
    scored = score_candidates(
        candidates,
        profile,
        season=season_for(as_of),
        as_of=as_of,
    )
    selected = rerank_for_diversity(scored, deck_size=DECK_SIZE)
    for rank, item in enumerate(selected, start=1):
        session.add(_deck_row(user.id, deck_date, rank, item))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


def _deck_row(
    user_id: UUID,
    deck_date: date,
    rank: int,
    item: ScoredRecommendation,
) -> RouteRecommendationDeckItem:
    return RouteRecommendationDeckItem(
        id=uuid4(),
        user_id=user_id,
        route_id=item.candidate.route_id,
        deck_date=deck_date,
        rank=rank,
        score=item.score,
        explanation_code=item.explanation_code,
        ranker_version=RANKER_VERSION,
        exploration=item.exploration,
    )


async def _load_profile(
    session: AsyncSession,
    *,
    user: User,
    as_of: datetime,
) -> RecommendationProfile:
    favorite_rows = list(
        (
            await session.execute(
                select(FavoriteRoute.route_id, Route.region_id)
                .join(Route, Route.id == FavoriteRoute.route_id)
                .where(FavoriteRoute.user_id == user.id)
            )
        ).all()
    )
    favorite_ids = frozenset(route_id for route_id, _region in favorite_rows)
    favorite_regions = frozenset(region_id for _route_id, region_id in favorite_rows)
    favorite_categories: set[str] = set()
    if favorite_ids:
        category_rows = (
            await session.execute(
                select(Category.slug)
                .join(PlaceCategory, PlaceCategory.category_id == Category.id)
                .join(RouteStop, RouteStop.place_id == PlaceCategory.place_id)
                .where(RouteStop.route_id.in_(favorite_ids))
            )
        ).all()
        favorite_categories = {slug for (slug,) in category_rows}

    skip_rows = (
        await session.scalars(
            select(RouteRecommendationFeedback.route_id).where(
                RouteRecommendationFeedback.user_id == user.id,
                RouteRecommendationFeedback.action == "skip",
                RouteRecommendationFeedback.created_at >= cooldown_cutoff(as_of=as_of),
            )
        )
    ).all()
    completed_rows = (
        await session.scalars(
            select(RouteExecution.route_id).where(
                RouteExecution.user_id == user.id,
                RouteExecution.status == "completed",
                RouteExecution.route_id.is_not(None),
                RouteExecution.completed_at.is_not(None),
                RouteExecution.completed_at >= completed_cutoff(as_of=as_of),
            )
        )
    ).all()
    return RecommendationProfile(
        preferred_categories=frozenset(user.preferred_categories or ()),
        preferred_difficulty=user.preferred_difficulty,
        travels_with_kids=bool(user.travels_with_kids),
        travels_with_pets=bool(user.travels_with_pets),
        favorite_route_ids=favorite_ids,
        favorite_category_slugs=frozenset(favorite_categories),
        favorite_region_ids=favorite_regions,
        skipped_route_ids=frozenset(skip_rows),
        completed_route_ids=frozenset(item for item in completed_rows if item is not None),
    )


async def _load_candidates(session: AsyncSession) -> list[RecommendationCandidate]:
    routes = list(
        (
            await session.scalars(
                select(Route)
                .where(*_PUBLIC_CATALOG, ~_has_unpublished_stop())
                .order_by(Route.updated_at.desc(), Route.id)
                .limit(CANDIDATE_LIMIT)
            )
        ).all()
    )
    if not routes:
        return []
    route_ids = [route.id for route in routes]
    favorite_counts: dict[UUID, int] = {
        route_id: int(count)
        for route_id, count in (
            await session.execute(
                select(FavoriteRoute.route_id, func.count())
                .where(FavoriteRoute.route_id.in_(route_ids))
                .group_by(FavoriteRoute.route_id)
            )
        ).all()
    }
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
    return [
        RecommendationCandidate(
            route_id=route.id,
            region_id=route.region_id,
            category_slugs=frozenset(categories_by_route.get(route.id, ())),
            difficulty=route.difficulty,
            suitable_for_children=route.suitable_for_children,
            pets_allowed=route.pets_allowed,
            created_at=route.created_at,
            favorite_count=int(favorite_counts.get(route.id, 0)),
            seasonality=tuple(route.seasonality or ()),
            quality_status=_quality_status(route.accessibility),
        )
        for route in routes
    ]
