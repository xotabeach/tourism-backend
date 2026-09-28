"""Collect one user's achievement evidence without holding their row lock."""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.achievements.facts import PLACE_SLUGS, Facts, RunFact, StopFact
from tourism_backend.modules.content.infrastructure.models import Article, ArticleLike
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.geography.infrastructure.models import Locality
from tourism_backend.modules.identity.infrastructure.models import ProfileLike
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceCategory,
    PlaceReview,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionEvent,
    RouteExecutionStop,
    RouteRoutingSnapshot,
    UserFraudState,
)
from tourism_backend.modules.routes.application.difficulty import MAX_LEVEL
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview, RouteStop


async def collect(session: AsyncSession, user_id: UUID, *, historical: bool = False) -> Facts:
    facts = Facts()
    facts.flagged = bool(
        await session.scalar(
            select(UserFraudState.is_flagged).where(UserFraudState.user_id == user_id)
        )
    )
    available_slugs = set(
        (
            await session.scalars(
                select(Place.slug).where(Place.slug.in_(set().union(*PLACE_SLUGS.values())))
            )
        ).all()
    )
    facts.soon = {key for key, slugs in PLACE_SLUGS.items() if not slugs & available_slugs}
    # Spec 17 (D17): the estimate, never an author's rating.
    extreme_exists = await session.scalar(
        select(Route.id)
        .where(
            Route.difficulty_reward == MAX_LEVEL,
            Route.publication_status == "published",
            Route.visibility == "public",
            Route.lifecycle_status == "active",
        )
        .limit(1)
    )
    if extreme_exists is None:
        facts.soon.add("legend-path")
    facts.counters["favorite"] = float(
        await session.scalar(
            select(func.count()).select_from(FavoriteRoute).where(FavoriteRoute.user_id == user_id)
        )
        or 0
    )
    facts.counters["social"] = float(
        await session.scalar(
            select(func.count()).select_from(ProfileLike).where(ProfileLike.liker_id == user_id)
        )
        or 0
    )
    reviews = 0
    for model in (PlaceReview, RouteReview):
        reviews += int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.author_user_id == user_id,
                    model.status == "published",
                    model.reply_to_review_id.is_(None),
                    # A bare star rating (FRONTEND-42) is not a review.
                    model.body != "",
                )
            )
            or 0
        )
    facts.counters["review"] = reviews
    facts.counters["photo"] = float(
        await session.scalar(
            select(func.count())
            .select_from(MediaAttachment)
            .join(PlaceReview, MediaAttachment.entity_id == PlaceReview.id)
            .where(
                MediaAttachment.entity_type == "place_review",
                MediaAttachment.status == "active",
                MediaAttachment.uploaded_by_user_id == user_id,
                MediaAttachment.content_type.like("image/%"),
                PlaceReview.author_user_id == user_id,
                PlaceReview.status == "published",
                PlaceReview.reply_to_review_id.is_(None),
            )
        )
        or 0
    )
    public_routes = select(Route.id).where(
        Route.owner_user_id == user_id,
        Route.publication_status == "published",
        Route.visibility == "public",
        Route.lifecycle_status == "active",
    )
    facts.counters["author"] = float(await session.scalar(public_routes.limit(1)) is not None)
    facts.counters["photographer"] = float(
        await session.scalar(
            select(MediaAttachment.id)
            .where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id.in_(public_routes),
                MediaAttachment.role == "cover",
                MediaAttachment.status == "active",
                MediaAttachment.uploaded_by_user_id == user_id,
                MediaAttachment.content_type.like("image/%"),
            )
            .limit(1)
        )
        is not None
    )
    articles = select(Article.id).where(
        Article.author_user_id == user_id, Article.status == "published"
    )
    facts.counters["pen"] = float(await session.scalar(articles.limit(1)) is not None)
    facts.counters["people-author"] = float(
        await session.scalar(
            select(func.count())
            .select_from(ArticleLike)
            .where(ArticleLike.article_id.in_(articles))
        )
        or 0
    )
    if historical:
        for slug, created_column, owner in [
            ("favorite", FavoriteRoute.created_at, FavoriteRoute.user_id),
            ("social", ProfileLike.created_at, ProfileLike.liker_id),
        ]:
            facts.counter_times[slug] = list(
                (await session.scalars(select(created_column).where(owner == user_id))).all()
            )
        facts.counter_times["review"] = []
        for model in (PlaceReview, RouteReview):
            times = (
                await session.scalars(
                    select(model.moderated_at).where(
                        model.author_user_id == user_id,
                        model.status == "published",
                        model.reply_to_review_id.is_(None),
                        model.body != "",
                        model.moderated_at.is_not(None),
                    )
                )
            ).all()
            facts.counter_times["review"].extend(at for at in times if at is not None)
        facts.counter_times["pen"] = [
            at
            for at in (
                await session.scalars(
                    select(Article.published_at).where(
                        Article.id.in_(articles), Article.published_at.is_not(None)
                    )
                )
            ).all()
            if at is not None
        ]
        facts.counter_times["people-author"] = list(
            (
                await session.scalars(
                    select(func.greatest(ArticleLike.created_at, Article.published_at))
                    .join(Article, Article.id == ArticleLike.article_id)
                    .where(Article.id.in_(articles))
                )
            ).all()
        )
    if facts.flagged:
        return facts

    eligible_runs = select(RouteExecution.id).where(
        RouteExecution.user_id == user_id,
        RouteExecution.status == "completed",
        RouteExecution.points_status.not_in(("held", "rejected")),
    )
    # Stop-only awards can be earned before finishing; held/rejected runs are
    # excluded, and the global user flag gates all grants in the service.
    stop_runs = select(RouteExecution.id).where(
        RouteExecution.user_id == user_id, RouteExecution.points_status.not_in(("held", "rejected"))
    )
    stop_times = (
        select(
            RouteExecutionEvent.stop_id.label("stop_id"),
            func.max(RouteExecutionEvent.recorded_at).label("recorded_at"),
        )
        .where(
            RouteExecutionEvent.user_id == user_id,
            RouteExecutionEvent.action == "complete_stop",
            RouteExecutionEvent.applied.is_(True),
        )
        .group_by(RouteExecutionEvent.stop_id)
        .subquery()
    )
    stop_rows = await session.execute(
        select(RouteExecutionStop, Place.slug, Locality.slug, stop_times.c.recorded_at)
        .outerjoin(Place, Place.id == RouteExecutionStop.place_id)
        .outerjoin(Locality, Locality.id == Place.locality_id)
        .outerjoin(stop_times, stop_times.c.stop_id == RouteExecutionStop.id)
        .where(
            RouteExecutionStop.execution_id.in_(stop_runs),
            RouteExecutionStop.completed_at.is_not(None),
        )
    )
    by_run: dict[UUID, list[StopFact]] = defaultdict(list)
    for stop, slug, locality, recorded in stop_rows:
        fact = StopFact(
            stop.place_id,
            slug,
            locality,
            stop.lat,
            stop.lng,
            stop.device_distance_m,
            recorded,
            stop.position,
        )
        by_run[stop.execution_id].append(fact)
        facts.stops.append(fact)
    completion_times = (
        select(
            RouteExecutionEvent.execution_id.label("execution_id"),
            func.min(RouteExecutionEvent.recorded_at).label("recorded_at"),
        )
        .where(
            RouteExecutionEvent.user_id == user_id,
            RouteExecutionEvent.action == "complete",
            RouteExecutionEvent.applied.is_(True),
        )
        .group_by(RouteExecutionEvent.execution_id)
        .subquery()
    )
    beach_routes = (
        select(RouteStop.route_id)
        .join(PlaceCategory, PlaceCategory.place_id == RouteStop.place_id)
        .join(Category, Category.id == PlaceCategory.category_id)
        .where(Category.slug == "beach")
    )
    rows = await session.execute(
        select(
            RouteExecution,
            RouteRoutingSnapshot.distance_meters,
            RouteRoutingSnapshot.difficulty_reward,
            RouteRoutingSnapshot.difficulty,
            Route,
            completion_times.c.recorded_at,
            RouteExecution.route_id.in_(beach_routes),
        )
        .outerjoin(
            RouteRoutingSnapshot, RouteRoutingSnapshot.id == RouteExecution.routing_snapshot_id
        )
        .outerjoin(Route, Route.id == RouteExecution.route_id)
        .outerjoin(completion_times, completion_times.c.execution_id == RouteExecution.id)
        .where(RouteExecution.id.in_(eligible_runs))
    )
    for run, meters, reward_level, snapshot_word, route, recorded, seaside in rows:
        # The estimate the run started with; runs from before spec 17 go by
        # the word their snapshot kept, else the route's (D17).
        if reward_level is not None:
            extreme = reward_level == MAX_LEVEL
        else:
            word = snapshot_word or (route.difficulty if route is not None else None)
            extreme = word == "extreme"
        facts.runs.append(
            RunFact(
                run.route_id,
                recorded,
                meters or 0,
                bool(seaside),
                extreme,
                route is not None
                and route.source == "generated"
                and route.owner_user_id == user_id,
                tuple(by_run[run.id]),
            )
        )
    return facts
