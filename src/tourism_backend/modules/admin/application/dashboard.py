"""Role-scoped counts for the admin landing page."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.app_stats.application.common import moscow_today
from tourism_backend.modules.app_stats.application.queries import StatsReport, build_report
from tourism_backend.modules.content.infrastructure.models import Article, ArticleComment
from tourism_backend.modules.moderation.infrastructure.models import ContentReport
from tourism_backend.modules.routes.infrastructure.models import Route
from tourism_backend.modules.support.infrastructure.models import SupportTicket

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass
class DashboardReport:
    updated_at: datetime
    counts: dict[str, int] = field(default_factory=dict)
    stats: StatsReport | None = None


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


async def build_dashboard(session: AsyncSession, *, permissions: frozenset[str]) -> DashboardReport:
    """Query only sections visible to this employee."""
    report = DashboardReport(updated_at=datetime.now(MOSCOW))
    counts = report.counts

    if "support.read" in permissions:
        counts["support_awaiting"] = await _count(
            session,
            select(func.count(SupportTicket.id)).where(
                SupportTicket.status != "closed", SupportTicket.last_human_author == "user"
            ),
        )
        counts["support_open"] = await _count(
            session,
            select(func.count(SupportTicket.id)).where(SupportTicket.status != "closed"),
        )

    if "routes.read" in permissions:
        counts["routes_pending"] = await _count(
            session,
            select(func.count(Route.id)).where(Route.publication_status == "pending_review"),
        )

    if "content.read" in permissions:
        counts["articles_pending"] = await _count(
            session, select(func.count(Article.id)).where(Article.status == "pending_review")
        )
        counts["comments_pending"] = await _count(
            session,
            select(func.count(ArticleComment.id)).where(ArticleComment.status == "pending_review"),
        )

    if "moderation_route.read" in permissions:
        counts["reports_route_open"] = await _count(
            session,
            select(func.count(ContentReport.id)).where(
                ContentReport.target_type.in_(("route", "place")),
                ContentReport.status.in_(("new", "in_review")),
            ),
        )
    if "moderation_content.read" in permissions:
        counts["reports_content_open"] = await _count(
            session,
            select(func.count(ContentReport.id)).where(
                ContentReport.target_type.in_(("article", "article_comment")),
                ContentReport.status.in_(("new", "in_review")),
            ),
        )

    if "statistics.read" in permissions:
        report.stats = await build_report(session, today=moscow_today(), days=7, new_from_build=1)
    return report
