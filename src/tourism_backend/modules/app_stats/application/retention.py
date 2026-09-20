"""Roll per-user version rows into daily aggregates, then drop old rows."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tourism_backend.modules.app_stats.infrastructure.models import (
    AppVersionDaily,
    AppVersionDailyUser,
)

USER_ROWS_RETENTION_DAYS = 35


def aggregate_complete_days(session: Session, *, today: date) -> int:
    """Upserts aggregates for every day before ``today`` still in the user table."""
    grouped = (
        select(
            AppVersionDailyUser.day,
            AppVersionDailyUser.app_version,
            AppVersionDailyUser.build_number,
            AppVersionDailyUser.platform,
            func.count().label("users"),
        )
        .where(AppVersionDailyUser.day < today)
        .group_by(
            AppVersionDailyUser.day,
            AppVersionDailyUser.app_version,
            AppVersionDailyUser.build_number,
            AppVersionDailyUser.platform,
        )
    )
    stmt = insert(AppVersionDaily).from_select(
        ["day", "app_version", "build_number", "platform", "users"], grouped
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["day", "app_version", "build_number", "platform"],
            set_={"users": stmt.excluded.users},
        )
    )
    return int(
        session.scalar(
            select(func.count()).select_from(AppVersionDaily).where(AppVersionDaily.day < today)
        )
        or 0
    )


def purge_user_rows(session: Session, *, today: date, apply: bool) -> int:
    cutoff = today - timedelta(days=USER_ROWS_RETENTION_DAYS)
    count = int(
        session.scalar(
            select(func.count())
            .select_from(AppVersionDailyUser)
            .where(AppVersionDailyUser.day < cutoff)
        )
        or 0
    )
    if apply and count:
        session.execute(delete(AppVersionDailyUser).where(AppVersionDailyUser.day < cutoff))
    return count
