"""How walkers found the difficulty they were promised (spec 17, section 7)."""

from __future__ import annotations

from typing import Any, ClassVar

from sqladmin import BaseView, expose
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.modules.admin.presentation.auth import require_permission
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteRoutingSnapshot,
)
from tourism_backend.modules.routes.infrastructure.models import Route

ANSWERS = ("easier", "as_expected", "harder")
ANSWER_LABELS = {"easier": "Легче", "as_expected": "Как ожидал", "harder": "Сложнее"}
# A route is flagged when this share of at least this many answers says
# «сложнее».
HARDER_SHARE = 0.4
MIN_ANSWERS = 5


def summarize(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """rows: (route_id, route_name, level, answer). Pure, for tests."""
    by_level: dict[int | None, dict[str, int]] = {}
    by_route: dict[Any, dict[str, Any]] = {}
    for route_id, name, level, answer in rows:
        counts = by_level.setdefault(level, dict.fromkeys(ANSWERS, 0))
        counts[answer] += 1
        entry = by_route.setdefault(
            route_id, {"name": name, "level": level, **dict.fromkeys(ANSWERS, 0)}
        )
        entry[answer] += 1
    flagged = []
    for route_id, entry in by_route.items():
        total = sum(entry[answer] for answer in ANSWERS)
        if total >= MIN_ANSWERS and entry["harder"] / total > HARDER_SHARE:
            flagged.append({"route_id": route_id, "total": total, **entry})
    flagged.sort(key=lambda item: item["harder"] / item["total"], reverse=True)
    levels = [
        {"level": level, "total": sum(counts.values()), **counts}
        for level, counts in sorted(by_level.items(), key=lambda item: item[0] or 0)
    ]
    return {"levels": levels, "flagged": flagged, "answers": len(rows)}


class DifficultyFeedbackAdmin(BaseView):
    name = "Сложность: отзывы"
    category = "Маршруты"
    icon = "fa-solid fa-mountain"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "routes.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/difficulty-feedback", methods=["GET"], identity="difficulty-feedback")
    async def report(self, request: Request) -> Response:
        if not require_permission(request, "routes.read"):
            return Response(status_code=403)
        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        RouteExecution.route_id,
                        func.coalesce(Route.name, RouteExecution.route_name),
                        RouteRoutingSnapshot.difficulty_reward,
                        RouteExecution.difficulty_feedback,
                    )
                    .outerjoin(Route, Route.id == RouteExecution.route_id)
                    .outerjoin(
                        RouteRoutingSnapshot,
                        RouteRoutingSnapshot.id == RouteExecution.routing_snapshot_id,
                    )
                    .where(RouteExecution.difficulty_feedback.is_not(None))
                )
            ).all()
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/difficulty_feedback.html",
            {**summarize([tuple(row) for row in rows]), "labels": ANSWER_LABELS},
        )
