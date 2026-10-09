"""Places walkers passed by as closed or too hard (spec 15, D4).

A skip reason is a one-tap answer given on the way. One answer proves
little; several from different people about the same place are a signal for
the editors to check the place or the route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqladmin import BaseView, expose
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.modules.admin.presentation.auth import require_permission
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)

#: The reasons that say something about the place, not about the walker.
SIGNAL_REASONS = ("closed", "hard")
REASON_LABELS = {"closed": "Закрыто", "hard": "Трудно или опасно"}
#: How far back skips are counted, and how many different people it takes.
WINDOW_DAYS = 90
MIN_PEOPLE = 3


def summarize(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """rows: (place_id, place_name, reason, user_id, skipped_at). Pure, for tests.

    One person counts once per place and reason, however many times they
    skipped it. A place is listed when enough different people gave the same
    reason; places below that are only counted.
    """

    by_place: dict[tuple[Any, str], dict[str, Any]] = {}
    for place_id, name, reason, user_id, skipped_at in rows:
        if reason not in SIGNAL_REASONS:
            continue
        entry = by_place.setdefault(
            (place_id, reason),
            {"place_id": place_id, "name": name, "reason": reason, "people": set(), "last": None},
        )
        entry["people"].add(user_id)
        if entry["last"] is None or skipped_at > entry["last"]:
            entry["last"] = skipped_at
    listed = [
        {**entry, "people": len(entry["people"]), "label": REASON_LABELS[entry["reason"]]}
        for entry in by_place.values()
        if len(entry["people"]) >= MIN_PEOPLE
    ]
    listed.sort(key=lambda item: (item["people"], item["last"]), reverse=True)
    return {
        "places": listed,
        "below": sum(1 for entry in by_place.values() if len(entry["people"]) < MIN_PEOPLE),
        "window_days": WINDOW_DAYS,
        "min_people": MIN_PEOPLE,
    }


class SkipSignalsAdmin(BaseView):
    name = "Точки: закрыто или опасно"
    category = "Маршруты"
    icon = "fa-solid fa-triangle-exclamation"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "routes.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/skip-signals", methods=["GET"], identity="skip-signals")
    async def report(self, request: Request) -> Response:
        if not require_permission(request, "routes.read"):
            return Response(status_code=403)
        since = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        RouteExecutionStop.place_id,
                        Place.name,
                        RouteExecutionStop.skip_reason,
                        RouteExecution.user_id,
                        RouteExecutionStop.skipped_at,
                    )
                    .join(RouteExecution, RouteExecution.id == RouteExecutionStop.execution_id)
                    .join(Place, Place.id == RouteExecutionStop.place_id)
                    .where(
                        RouteExecutionStop.skip_reason.in_(SIGNAL_REASONS),
                        RouteExecutionStop.skipped_at >= since,
                        # Held and rejected runs are the anti-fraud's doubt
                        # about the person; their word is not a signal.
                        RouteExecution.points_status.notin_(("held", "rejected")),
                    )
                )
            ).all()
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/skip_signals.html",
            summarize([tuple(row) for row in rows]),
        )
