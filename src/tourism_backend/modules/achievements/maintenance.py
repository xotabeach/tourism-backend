"""Hourly catch-up and a repeatable, silent historical recalculation."""

import csv
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, delete, select, union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import CompoundSelect

from tourism_backend.modules.achievements.service import evaluate
from tourism_backend.modules.content.infrastructure.models import Article, ArticleLike
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.identity.infrastructure.models import (
    ProfileLike,
    User,
    UserAchievement,
)
from tourism_backend.modules.notifications.infrastructure.models import Notification
from tourism_backend.modules.places.infrastructure.models import PlaceReview
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    UserFraudState,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview


def active_user_ids(since: datetime) -> CompoundSelect[Any]:
    return union(
        select(User.id).where(User.updated_at >= since),
        select(RouteExecution.user_id).where(RouteExecution.updated_at >= since),
        select(UserFraudState.user_id).where(UserFraudState.updated_at >= since),
        select(FavoriteRoute.user_id).where(FavoriteRoute.created_at >= since),
        select(ProfileLike.liker_id).where(ProfileLike.created_at >= since),
        select(PlaceReview.author_user_id).where(PlaceReview.updated_at >= since),
        select(RouteReview.author_user_id).where(RouteReview.updated_at >= since),
        select(Route.owner_user_id).where(
            Route.updated_at >= since, Route.owner_user_id.is_not(None)
        ),
        select(Article.author_user_id).where(Article.updated_at >= since),
        select(Article.author_user_id)
        .join(ArticleLike, ArticleLike.article_id == Article.id)
        .where(ArticleLike.created_at >= since),
    )


async def export_before_reset(session: AsyncSession, backup_dir: Path) -> Path:
    """Exclusive files, fsync before deleting anything, and private permissions."""
    directory = backup_dir / datetime.now(UTC).strftime("achievements-%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, mode=0o700)
    for table, condition in [
        (UserAchievement.__table__, None),
        (Notification.__table__, Notification.kind == "achievement_unlocked"),
    ]:
        table = cast(Table, table)
        query = select(table)
        if condition is not None:
            query = query.where(condition)
        rows = (await session.execute(query)).mappings()
        path = directory / f"{table.name}.csv"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=list(table.columns.keys()))
            writer.writeheader()
            writer.writerows({str(k): v for k, v in row.items()} for row in rows)
            out.flush()
            os.fsync(out.fileno())
    return directory


async def backfill(
    session: AsyncSession,
    *,
    reset: bool = False,
    backup_dir: Path | None = None,
    user_ids: Sequence[UUID] | None = None,
) -> int:
    """Reset always covers every grant; ``user_ids`` only narrows re-evaluation."""
    if reset:
        if backup_dir is None:
            raise ValueError("A backup directory is required before resetting grants")
        await export_before_reset(session, backup_dir)
        await session.execute(
            delete(Notification).where(Notification.kind == "achievement_unlocked")
        )
        await session.execute(delete(UserAchievement))
    count = 0
    users = (
        list(user_ids)
        if user_ids is not None
        else list((await session.scalars(select(User.id).order_by(User.id))).all())
    )
    for user_id in users:
        count += len(await evaluate(session, user_id, backfill=True))
    return count


def recent_cutoff(hours: int = 2) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)
