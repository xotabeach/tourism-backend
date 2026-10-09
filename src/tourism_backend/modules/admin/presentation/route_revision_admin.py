"""What an edit changes in a published route, for the moderator (spec 15, D6)."""

from __future__ import annotations

import contextlib
from typing import Any, ClassVar
from uuid import UUID

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.modules.admin.presentation.auth import require_permission
from tourism_backend.modules.routes.application.route_revisions import revision_diff
from tourism_backend.modules.routes.infrastructure.models import Route

#: From this share of removed stops the page suggests «опубликуйте как новый
#: маршрут»: the reviews and the rating were given to a different walk.
NEW_ROUTE_HINT_PERCENT = 50


class RouteRevisionAdmin(BaseView):
    name = "Правка маршрута: сравнение версий"
    category = "Маршруты"
    icon = "fa-solid fa-code-compare"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "routes.read")

    def is_visible(self, request: Request) -> bool:
        # Opened from an edit's row in the routes list, not from the menu.
        return False

    @expose("/route-revision", methods=["GET"], identity="route-revision")
    async def compare(self, request: Request) -> Response:
        if not require_permission(request, "routes.read"):
            return Response(status_code=403)
        route_id: UUID | None = None
        with contextlib.suppress(ValueError):
            route_id = UUID(request.query_params.get("route_id", ""))
        context: dict[str, Any] = {"revision": None, "diff": None}
        if route_id is not None:
            async with self.session_maker() as session:
                revision = await session.get(Route, route_id)
                if revision is not None and revision.revision_of_route_id is not None:
                    diff = await revision_diff(session, revision)
                    context = {
                        "revision": revision,
                        "diff": diff,
                        "suggest_new_route": diff is not None
                        and diff.stops_changed_share >= NEW_ROUTE_HINT_PERCENT,
                    }
        return await self.templates.TemplateResponse(
            request, "sqladmin/route_revision.html", context
        )
