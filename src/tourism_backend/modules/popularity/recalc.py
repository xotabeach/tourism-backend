"""Nightly recalculation of popularity (spec 19, steps 19-4 and 19-5).

Reads the last 90 days of what people did, scores places and routes with
``scoring`` and writes the result back. Synchronous on purpose: it runs from
``scripts/recalculate_popularity.py``, never inside a request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from tourism_backend.modules.favorites.infrastructure.models import FavoritePlace, FavoriteRoute
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place, PlaceViewEvent
from tourism_backend.modules.popularity.policy import (
    MIN_ACCOUNT_AGE_DAYS,
    PLACE_WEIGHT_FAVORITE,
    PLACE_WEIGHT_VIEWED,
    PLACE_WEIGHT_VISITED,
    ROUTE_WEIGHT_COMPLETED,
    ROUTE_WEIGHT_FAVORITE,
    ROUTE_WEIGHT_REVIEW,
    ROUTE_WEIGHT_STARTED,
    WINDOW_DAYS,
)
from tourism_backend.modules.popularity.scoring import (
    Action,
    blend_place,
    entity_scores,
    percentile_scores,
    popular_places,
    popular_routes,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview

#: Runs the anti-fraud held back or an operator rejected are not honest (D45).
_DISHONEST_POINTS = ("held", "rejected")


@dataclass(frozen=True)
class EntityResult:
    popularity: float
    people: int
    is_popular: bool


@dataclass
class PopularityReport:
    eligible_users: int = 0
    routes_published: int = 0
    places_published: int = 0
    routes: dict[UUID, EntityResult] = field(default_factory=dict)
    places: dict[UUID, EntityResult] = field(default_factory=dict)

    @property
    def popular_routes(self) -> int:
        return sum(1 for result in self.routes.values() if result.is_popular)

    @property
    def popular_places(self) -> int:
        return sum(1 for result in self.places.values() if result.is_popular)


def eligible_user_ids(session: Session, *, now: datetime) -> set[UUID]:
    """People whose actions count: not a service or team account, not a new one."""

    created_before = now - timedelta(days=MIN_ACCOUNT_AGE_DAYS)
    return set(
        session.scalars(
            select(User.id).where(
                User.is_system_account.is_(False),
                User.is_internal_account.is_(False),
                User.created_at <= created_before,
            )
        )
    )


def _route_actions(session: Session, *, since: datetime) -> list[Action]:
    actions: list[Action] = []
    for user_id, route_id, at in session.execute(
        select(RouteExecution.user_id, RouteExecution.route_id, RouteExecution.completed_at).where(
            RouteExecution.status == "completed",
            RouteExecution.points_status.notin_(_DISHONEST_POINTS),
            RouteExecution.route_id.is_not(None),
            RouteExecution.completed_at >= since,
        )
    ):
        actions.append(Action(user_id, route_id, ROUTE_WEIGHT_COMPLETED, at))
    for user_id, route_id, at in session.execute(
        select(RouteExecution.user_id, RouteExecution.route_id, RouteExecution.started_at).where(
            RouteExecution.route_id.is_not(None),
            RouteExecution.started_at >= since,
        )
    ):
        actions.append(Action(user_id, route_id, ROUTE_WEIGHT_STARTED, at))
    for user_id, route_id, at in session.execute(
        select(RouteReview.author_user_id, RouteReview.route_id, RouteReview.created_at).where(
            RouteReview.status == "published",
            RouteReview.reply_to_review_id.is_(None),
            RouteReview.created_at >= since,
        )
    ):
        actions.append(Action(user_id, route_id, ROUTE_WEIGHT_REVIEW, at))
    for user_id, route_id, at in session.execute(
        select(FavoriteRoute.user_id, FavoriteRoute.route_id, FavoriteRoute.created_at).where(
            FavoriteRoute.created_at >= since
        )
    ):
        actions.append(Action(user_id, route_id, ROUTE_WEIGHT_FAVORITE, at))
    return actions


def _place_actions(session: Session, *, since: datetime) -> list[Action]:
    actions: list[Action] = []
    for user_id, place_id, at in session.execute(
        select(
            RouteExecution.user_id,
            RouteExecutionStop.place_id,
            RouteExecutionStop.completed_at,
        )
        .join(RouteExecution, RouteExecution.id == RouteExecutionStop.execution_id)
        .where(
            RouteExecutionStop.place_id.is_not(None),
            RouteExecutionStop.completed_at >= since,
            RouteExecutionStop.mark_below_floor.is_(False),
            RouteExecution.points_status.notin_(_DISHONEST_POINTS),
        )
    ):
        actions.append(Action(user_id, place_id, PLACE_WEIGHT_VISITED, at))
    for user_id, place_id, at in session.execute(
        select(FavoritePlace.user_id, FavoritePlace.place_id, FavoritePlace.created_at).where(
            FavoritePlace.created_at >= since
        )
    ):
        actions.append(Action(user_id, place_id, PLACE_WEIGHT_FAVORITE, at))
    for user_id, place_id, day in session.execute(
        select(PlaceViewEvent.user_id, PlaceViewEvent.place_id, PlaceViewEvent.day).where(
            PlaceViewEvent.day >= since.date()
        )
    ):
        # A view carries a day, not a moment: noon keeps it inside that day
        # in any time zone the server may run in.
        at = datetime.combine(day, time(12, 0), tzinfo=UTC)
        actions.append(Action(user_id, place_id, PLACE_WEIGHT_VIEWED, at))
    return actions


def compute(session: Session, *, now: datetime | None = None) -> PopularityReport:
    """Score every published place and route. Reads only."""

    now = now or datetime.now(UTC)
    since = now - timedelta(days=WINDOW_DAYS)
    eligible = eligible_user_ids(session, now=now)
    report = PopularityReport(eligible_users=len(eligible))

    route_ids = set(
        session.scalars(
            select(Route.id).where(
                Route.publication_status == "published",
                Route.visibility == "public",
                Route.lifecycle_status == "active",
            )
        )
    )
    report.routes_published = len(route_ids)
    route_scores = {
        route_id: score
        for route_id, score in entity_scores(
            _route_actions(session, since=since), now=now, eligible_user_ids=eligible
        ).items()
        if route_id in route_ids
    }
    route_popularity = percentile_scores({rid: score.raw for rid, score in route_scores.items()})
    route_people = {rid: score.people for rid, score in route_scores.items()}
    badge_routes = popular_routes(route_popularity, route_people, population=len(route_ids))
    report.routes = {
        route_id: EntityResult(
            popularity=route_popularity[route_id],
            people=route_people[route_id],
            is_popular=route_id in badge_routes,
        )
        for route_id in route_popularity
    }

    # ``.all()`` matters: a Result has ``keys()``, so ``dict(result)`` would
    # take it for a mapping and try to subscript it.
    external: dict[UUID, float | None] = dict(
        session.execute(
            select(Place.id, Place.popularity_external).where(
                Place.publication_status == "published",
                Place.merged_into_place_id.is_(None),
            )
        )
        .tuples()
        .all()
    )
    report.places_published = len(external)
    place_scores = {
        place_id: score
        for place_id, score in entity_scores(
            _place_actions(session, since=since), now=now, eligible_user_ids=eligible
        ).items()
        if place_id in external
    }
    place_app = percentile_scores({pid: score.raw for pid, score in place_scores.items()})
    place_people = {pid: score.people for pid, score in place_scores.items()}
    has_external = {place_id for place_id, value in external.items() if value is not None}
    final = {
        place_id: blend_place(
            external=external[place_id],
            app=place_app.get(place_id, 0.0),
            people=place_people.get(place_id, 0),
        )
        for place_id in has_external | set(place_app)
    }
    badge_places = popular_places(final, place_people, has_external, population=len(external))
    report.places = {
        place_id: EntityResult(
            popularity=value,
            people=place_people.get(place_id, 0),
            is_popular=place_id in badge_places,
        )
        for place_id, value in final.items()
        if value > 0
    }
    return report


def apply(session: Session, report: PopularityReport, *, now: datetime | None = None) -> None:
    """Write a computed report. The caller commits."""

    now = now or datetime.now(UTC)
    for model, results in ((Route, report.routes), (Place, report.places)):
        stale = or_(
            model.popularity != 0,
            model.popularity_people != 0,
            model.is_popular.is_(True),
        )
        reset = update(model).where(stale)
        if results:
            reset = reset.where(model.id.notin_(list(results)))
        session.execute(
            reset.values(
                popularity=0, popularity_people=0, is_popular=False, popularity_updated_at=now
            )
        )
        if results:
            session.execute(
                update(model),
                [
                    {
                        "id": entity_id,
                        "popularity": result.popularity,
                        "popularity_people": result.people,
                        "is_popular": result.is_popular,
                        "popularity_updated_at": now,
                    }
                    for entity_id, result in results.items()
                ],
            )
