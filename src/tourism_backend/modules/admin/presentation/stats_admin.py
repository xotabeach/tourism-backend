"""Admin page «Статистика»: APK downloads and app-version share (read only)."""

from __future__ import annotations

from typing import Any, ClassVar

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.modules.admin.presentation.auth import session_roles
from tourism_backend.modules.app_stats.application.common import moscow_today
from tourism_backend.modules.app_stats.application.queries import build_report, normalize_period

STATS_ALLOWED_ROLES = frozenset({"ops", "admin"})
DEFAULT_NEW_FROM_BUILD = 1


def stats_role_allowed(request: Request) -> bool:
    return bool(STATS_ALLOWED_ROLES & set(session_roles(request)))


def parse_build(raw: str | None) -> int:
    try:
        return max(int(raw or ""), 1)
    except ValueError:
        return DEFAULT_NEW_FROM_BUILD


class StatsAdmin(BaseView):
    name = "Статистика"
    icon = "fa-solid fa-chart-column"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return stats_role_allowed(request)

    def is_visible(self, request: Request) -> bool:
        return stats_role_allowed(request)

    @expose("/stats", methods=["GET"], identity="stats")
    async def show(self, request: Request) -> Response:
        days = normalize_period(request.query_params.get("days"))
        new_from = parse_build(request.query_params.get("from_build"))
        async with self.session_maker(expire_on_commit=False) as session:
            report = await build_report(
                session, today=moscow_today(), days=days, new_from_build=new_from
            )
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/stats.html",
            context={"report": report, "periods": (7, 30, 90)},
        )
