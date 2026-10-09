"""Who walked a route, and whose stars enter its rating (spec 15, D3).

Anyone who walked at least a part of a route may review it. A review by
someone who walked less than the counting threshold is shown with a
«прошёл частично» mark and stays out of the average: they tell what they
met on the way, but cannot judge the whole route.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, exists, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.modules.route_execution.infrastructure.models import RouteExecution
from tourism_backend.modules.routes.infrastructure.models import RouteReview

WalkState = Literal["full", "partial"]


def _counted_run() -> ColumnElement[bool]:
    return and_(RouteExecution.status == "completed", RouteExecution.counted.is_(True))


def _partial_run() -> ColumnElement[bool]:
    # A run that ended with something walked but did not count: completed
    # below the threshold, or ended early. A plain cancel has no share.
    return and_(
        RouteExecution.counted.is_(False),
        RouteExecution.completed_share_percent > 0,
    )


def partial_only_review() -> ColumnElement[bool]:
    """True for a review whose author walked this route only in part."""

    same = (
        RouteExecution.route_id == RouteReview.route_id,
        RouteExecution.user_id == RouteReview.author_user_id,
    )
    return and_(
        exists().where(*same, _partial_run()),
        not_(exists().where(*same, _counted_run())),
    )


async def walk_states(
    session: AsyncSession, route_id: UUID, author_ids: Sequence[UUID]
) -> dict[UUID, WalkState]:
    """How each of these people walked the route, in one query.

    «full» wins: someone who once walked the route whole stays a walker
    whatever their other runs were. People who never walked it are absent.
    """

    if not author_ids:
        return {}
    rows = await session.execute(
        select(
            RouteExecution.user_id,
            func.bool_or(_counted_run()),
            func.bool_or(_partial_run()),
        )
        .where(RouteExecution.route_id == route_id, RouteExecution.user_id.in_(author_ids))
        .group_by(RouteExecution.user_id)
    )
    states: dict[UUID, WalkState] = {}
    for user_id, full, partial in rows:
        if full:
            states[user_id] = "full"
        elif partial:
            states[user_id] = "partial"
    return states
