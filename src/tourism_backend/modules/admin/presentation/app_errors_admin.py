"""«Ошибки приложения» (BACKEND-73, spec 47): what the app reported.

A summary of the last week by error type and screen, then the reports
themselves, newest first, filtered by person, kind and version. Behind its
own permission: a report says who had the error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from sqladmin import BaseView, expose
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.modules.admin.presentation.auth import require_permission
from tourism_backend.modules.app_stats.application.error_reports import RETENTION_DAYS
from tourism_backend.modules.app_stats.infrastructure.models import AppErrorReport
from tourism_backend.modules.identity.infrastructure.models import User

PAGE_ROWS = 100
KIND_LABELS = {
    "crash": "Сбой",
    "cache_read": "Не прочитались данные",
    "blank_screen": "Пустой экран",
}


class AppErrorsAdmin(BaseView):
    name = "Ошибки приложения"
    category = "Статистика"
    icon = "fa-solid fa-bug"
    #: Its own permission, «app_errors.read», not the category's.
    permission_scope = "app_errors"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "app_errors.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/app-errors", methods=["GET"], identity="app-errors")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "app_errors.read"):
            return Response(status_code=403)
        params = request.query_params
        kind = params.get("kind", "")
        version = params.get("version", "").strip()
        who = params.get("user", "").strip()
        user_id: UUID | None = None
        if who:
            try:
                user_id = UUID(who)
            except ValueError:
                user_id = None

        filters = []
        if kind in KIND_LABELS:
            filters.append(AppErrorReport.kind == kind)
        if version:
            filters.append(AppErrorReport.app_version == version)
        week = datetime.now(UTC) - timedelta(days=7)
        async with self.session_maker() as session:
            if who and user_id is None:
                # A name: everyone whose display name has it.
                ids = (
                    await session.scalars(
                        select(User.id).where(User.display_name.ilike(f"%{who}%")).limit(50)
                    )
                ).all()
                filters.append(AppErrorReport.user_id.in_(ids))
            elif user_id is not None:
                filters.append(AppErrorReport.user_id == user_id)
            summary = (
                await session.execute(
                    select(
                        AppErrorReport.kind,
                        AppErrorReport.error_type,
                        AppErrorReport.screen,
                        func.count().label("reports"),
                        func.count(func.distinct(AppErrorReport.user_id)).label("people"),
                        func.max(AppErrorReport.created_at).label("last"),
                    )
                    .where(AppErrorReport.created_at >= week, *filters)
                    .group_by(AppErrorReport.kind, AppErrorReport.error_type, AppErrorReport.screen)
                    .order_by(func.count().desc())
                    .limit(30)
                )
            ).all()
            rows = (
                await session.execute(
                    select(AppErrorReport, User.display_name)
                    .join(User, User.id == AppErrorReport.user_id)
                    .where(*filters)
                    .order_by(AppErrorReport.created_at.desc())
                    .limit(PAGE_ROWS)
                )
            ).all()
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/app_errors.html",
            {
                "summary": summary,
                "rows": rows,
                "kinds": KIND_LABELS,
                "kind": kind,
                "version": version,
                "who": who,
                "retention_days": RETENTION_DAYS,
                "page_rows": PAGE_ROWS,
            },
        )
