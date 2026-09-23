"""Achievement catalogue diagnostics and explicit audited grant/revoke form."""

from typing import Any, ClassVar
from uuid import UUID

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.admin_actions import apply
from tourism_backend.modules.achievements.queries import collect
from tourism_backend.modules.achievements.rules import RULES
from tourism_backend.modules.admin.presentation.auth import require_admin_role, session_principal_id
from tourism_backend.modules.identity.infrastructure.models import Achievement, UserAchievement


class AchievementsOperationsAdmin(BaseView):
    name = "Правила и ручная выдача"
    category = "Достижения"
    icon = "fa-solid fa-trophy"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)

    @expose("/achievement-operations", methods=["GET", "POST"], identity="achievement-operations")
    async def operations(self, request: Request) -> Response:
        admin_id = session_principal_id(request)
        if admin_id is None or not require_admin_role(request):
            return Response(status_code=403)
        async with self.session_maker(expire_on_commit=False) as session:
            if request.method == "POST":
                form = await request.form()
                try:
                    await apply(
                        session,
                        user_id=UUID(str(form.get("user_id", ""))),
                        achievement_id=UUID(str(form.get("achievement_id", ""))),
                        admin_id=admin_id,
                        action=str(form.get("action", "")),
                        reason=str(form.get("reason", "")),
                    )
                    Flash.success(request, "Действие сохранено в журнале.")
                except ValueError:
                    Flash.error(request, "Укажите корректные ID пользователя и достижения.")
                except AppError as exc:
                    Flash.error(request, exc.message)
                return RedirectResponse(
                    str(request.url_for("admin:achievement-operations")), status_code=303
                )
            facts = await collect(session, UUID(int=0))
            rows = (
                await session.execute(
                    select(Achievement, func.count(UserAchievement.user_id))
                    .outerjoin(UserAchievement, UserAchievement.achievement_id == Achievement.id)
                    .group_by(Achievement.id)
                    .order_by(Achievement.sort_order)
                )
            ).all()
            catalog = [
                {
                    "id": str(badge.id),
                    "title": badge.title,
                    "count": count,
                    "status": "Работает"
                    if badge.slug in RULES
                    and not RULES[badge.slug].soon
                    and badge.slug not in facts.soon
                    else "Скоро",
                }
                for badge, count in rows
            ]
        return await self.templates.TemplateResponse(
            request, "sqladmin/achievement_operations.html", {"catalog": catalog}
        )
