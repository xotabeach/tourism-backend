"""The app's error reports (BACKEND-73, spec 47).

The app sends uncaught errors, stored entries that would not read and a
route screen that never appeared. Only signed-in people send them, so every
report has an owner and a limit; errors from before sign-in wait on the
phone. Nothing personal goes in: the app sends the error's type and the
stack, never the error's text, and drops query strings from screen names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tourism_backend.api.errors import AppError
from tourism_backend.modules.app_stats.infrastructure.models import AppErrorReport

#: Reports kept this long, then dropped by the daily retention job.
RETENTION_DAYS = 30
#: Reports one account may send in an hour; the rest are refused.
HOURLY_LIMIT = 20
#: A batch the app sends at once, and its size on the wire.
MAX_BATCH = 20
MAX_BODY_BYTES = 64 * 1024
MAX_STACK = 8000

ErrorKind = Literal["crash", "cache_read", "blank_screen"]


class AppErrorIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: ErrorKind
    error_type: str = Field(min_length=1, max_length=120)
    stack: str | None = Field(default=None, max_length=MAX_STACK)
    screen: str | None = Field(default=None, max_length=120)
    app_version: str = Field(min_length=1, max_length=32)
    build_number: int = Field(ge=0, le=1_000_000)
    platform: str = Field(min_length=1, max_length=16)
    device_model: str | None = Field(default=None, max_length=80)
    os_version: str | None = Field(default=None, max_length=40)
    occurred_at: datetime


class AppErrorsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: list[AppErrorIn] = Field(min_length=1, max_length=MAX_BATCH)


class AppErrorsOut(BaseModel):
    accepted: int


def _screen(value: str | None) -> str | None:
    """A screen name without a query string, in case one slipped through."""

    if value is None:
        return None
    return value.split("?", 1)[0].split("#", 1)[0] or None


async def record_reports(
    session: AsyncSession, redis: Redis, *, user_id: UUID, payload: AppErrorsIn
) -> AppErrorsOut:
    key = f"app_errors:{user_id}"
    count = await redis.incrby(key, len(payload.reports))
    if count == len(payload.reports):
        await redis.expire(key, 3600)
    if count > HOURLY_LIMIT:
        raise AppError(
            code="rate_limited",
            message="Too many error reports. Try again later.",
            status_code=429,
        )
    now = datetime.now(UTC)
    for report in payload.reports:
        occurred = report.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        # A phone with a wrong clock must not file reports in the future.
        session.add(
            AppErrorReport(
                user_id=user_id,
                kind=report.kind,
                error_type=report.error_type,
                stack=report.stack,
                screen=_screen(report.screen),
                app_version=report.app_version,
                build_number=report.build_number,
                platform=report.platform,
                device_model=report.device_model,
                os_version=report.os_version,
                occurred_at=min(occurred, now),
                created_at=now,
            )
        )
    await session.commit()
    return AppErrorsOut(accepted=len(payload.reports))


def purge_reports(session: Session, *, today: date, apply: bool) -> int:
    """Reports older than [RETENTION_DAYS]; deleted only with ``apply``."""

    cutoff = datetime.combine(today - timedelta(days=RETENTION_DAYS), datetime.min.time(), UTC)
    count = int(
        session.scalar(
            select(func.count())
            .select_from(AppErrorReport)
            .where(AppErrorReport.created_at < cutoff)
        )
        or 0
    )
    if apply and count:
        session.execute(delete(AppErrorReport).where(AppErrorReport.created_at < cutoff))
    return count
