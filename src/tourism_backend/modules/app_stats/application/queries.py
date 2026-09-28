"""Read side of the stats page: downloads and version share."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.app_stats.application.retention import USER_ROWS_RETENTION_DAYS
from tourism_backend.modules.app_stats.infrastructure.models import (
    ApkDownloadDaily,
    AppVersionDaily,
    AppVersionDailyUser,
)

PERIODS = (7, 30, 90)


@dataclass
class VersionShareDay:
    day: date
    total: int
    fresh: int

    @property
    def percent(self) -> int:
        return round(self.fresh * 100 / self.total) if self.total else 0


@dataclass
class StatsReport:
    days: int
    new_from_build: int
    downloads_by_day: list[tuple[date, int]] = field(default_factory=list)
    downloads_by_source: list[tuple[str, int]] = field(default_factory=list)
    downloads_by_version: list[tuple[str, int]] = field(default_factory=list)
    downloads_total: int = 0
    share_day: date | None = None
    users_by_version: list[tuple[str, int, int]] = field(
        default_factory=list
    )  # version, build, users
    share_series: list[VersionShareDay] = field(default_factory=list)

    @property
    def max_day_downloads(self) -> int:
        return max((n for _, n in self.downloads_by_day), default=0)

    @property
    def display_share(self) -> VersionShareDay | None:
        return next((row for row in self.share_series if row.day == self.share_day), None)


def normalize_period(raw: str | None) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        return 30
    return value if value in PERIODS else 30


async def build_report(
    session: AsyncSession, *, today: date, days: int, new_from_build: int
) -> StatsReport:
    new_from_build = max(new_from_build, 1)  # build 0 is "unknown": never counts as new
    report = StatsReport(days=days, new_from_build=new_from_build)
    start = today - timedelta(days=days - 1)

    rows = (
        (
            await session.execute(
                select(
                    ApkDownloadDaily.day,
                    ApkDownloadDaily.source,
                    ApkDownloadDaily.apk_version,
                    ApkDownloadDaily.count,
                ).where(ApkDownloadDaily.day >= start)
            )
        )
        .tuples()
        .all()
    )
    by_day: dict[date, int] = {}
    by_source: dict[str, int] = {}
    by_version: dict[str, int] = {}
    for day, source, version, count in rows:
        by_day[day] = by_day.get(day, 0) + count
        by_source[source] = by_source.get(source, 0) + count
        by_version[version] = by_version.get(version, 0) + count
    report.downloads_by_day = [
        (start + timedelta(days=i), by_day.get(start + timedelta(days=i), 0)) for i in range(days)
    ]
    report.downloads_by_source = sorted(by_source.items(), key=lambda kv: -kv[1])
    report.downloads_by_version = sorted(by_version.items(), key=lambda kv: -kv[1])
    report.downloads_total = sum(by_day.values())

    live_from = today - timedelta(days=USER_ROWS_RETENTION_DAYS - 1)
    fresh_expr = func.count(func.distinct(AppVersionDailyUser.user_id)).filter(
        AppVersionDailyUser.build_number >= new_from_build
    )
    live = (
        (
            await session.execute(
                select(
                    AppVersionDailyUser.day,
                    func.count(func.distinct(AppVersionDailyUser.user_id)),
                    fresh_expr,
                )
                .where(AppVersionDailyUser.day >= max(start, live_from))
                .group_by(AppVersionDailyUser.day)
            )
        )
        .tuples()
        .all()
    )
    series: dict[date, VersionShareDay] = {
        d: VersionShareDay(d, int(total), int(fresh)) for d, total, fresh in live
    }
    if start < live_from:
        old = (
            (
                await session.execute(
                    select(
                        AppVersionDaily.day,
                        func.sum(AppVersionDaily.users),
                        func.coalesce(
                            func.sum(AppVersionDaily.users).filter(
                                AppVersionDaily.build_number >= new_from_build
                            ),
                            0,
                        ),
                    )
                    .where(AppVersionDaily.day >= start, AppVersionDaily.day < live_from)
                    .group_by(AppVersionDaily.day)
                )
            )
            .tuples()
            .all()
        )
        for d, total, fresh in old:
            series[d] = VersionShareDay(d, int(total), int(fresh))
    report.share_series = [series[d] for d in sorted(series)]

    # Last complete day (yesterday); before the first night, fall back to today.
    yesterday = today - timedelta(days=1)
    report.share_day = yesterday if yesterday in series else (today if today in series else None)
    if report.share_day is not None and report.share_day >= live_from:
        per_version = (
            (
                await session.execute(
                    select(
                        AppVersionDailyUser.app_version,
                        AppVersionDailyUser.build_number,
                        func.count(func.distinct(AppVersionDailyUser.user_id)),
                    )
                    .where(AppVersionDailyUser.day == report.share_day)
                    .group_by(AppVersionDailyUser.app_version, AppVersionDailyUser.build_number)
                    .order_by(AppVersionDailyUser.build_number.desc())
                )
            )
            .tuples()
            .all()
        )
        report.users_by_version = [(v, int(b), int(n)) for v, b, n in per_version]
    return report
