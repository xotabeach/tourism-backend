"""Editors' days and car parks of a route (spec 14a/14b, «ручные дни и парковки»)."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.presentation.auth import (
    require_permission,
    session_principal_id,
)
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.application.rerouting import reroute_route
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)

_MODES = {"walk": "пешком", "car": "на машине"}
_ROLES = {"approach": " от парковки", "return": " обратно к машине", "main": ""}


def parse_point(raw: str) -> tuple[float, float] | None:
    """«44.742, 33.9205» as copied from a map (lat, lng) → (lng, lat)."""
    parts = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError(raw)
    lat, lng = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(raw)
    return lng, lat


class RouteStructureAdmin(BaseView):
    name = "Дни и парковки маршрута"
    category = "Маршруты"
    icon = "fa-solid fa-bed"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "routes.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/route-structure", methods=["GET", "POST"], identity="route-structure")
    async def structure(self, request: Request) -> Response:
        needed = "routes.write" if request.method == "POST" else "routes.read"
        if not require_permission(request, needed):
            return Response(status_code=403)
        raw_id = request.query_params.get("route_id", "").strip()
        route_id: UUID | None = None
        if raw_id:
            try:
                route_id = UUID(raw_id)
            except ValueError:
                Flash.error(request, "Укажите корректный ID маршрута.")
        async with self.session_maker(expire_on_commit=False) as session:
            route = await session.get(Route, route_id) if route_id else None
            if route_id and route is None:
                Flash.error(request, "Маршрут не найден.")
            stops = (
                (
                    await session.execute(
                        select(RouteStop.id, Place.id, Place.name)
                        .join(Place, Place.id == RouteStop.place_id)
                        .where(RouteStop.route_id == route.id)
                        .order_by(RouteStop.position)
                    )
                ).all()
                if route
                else []
            )
            if request.method == "POST" and route is not None:
                form = await request.form()
                try:
                    overrides: dict[str, list[float]] = {}
                    for _stop_id, place_id, _name in stops:
                        raw = str(form.get(f"parking_{place_id}", "")).strip()
                        if raw:
                            point = parse_point(raw)
                            if point is not None:
                                overrides[str(place_id)] = [point[0], point[1]]
                except ValueError:
                    Flash.error(
                        request, "Парковка: широта и долгота через запятую, например 44.742, 33.92."
                    )
                    return RedirectResponse(
                        f"{request.url.path}?route_id={route.id}",
                        status_code=303,
                    )
                breaks = [
                    str(place_id)
                    for _stop_id, place_id, _name in stops[:-1]
                    if form.get(f"break_{place_id}") == "on"
                ]
                route.day_breaks = breaks or None
                route.days_manual = bool(breaks)
                route.parking_overrides = overrides or None
                result = await reroute_route(session, route)
                await record_audit(
                    session,
                    actor_id=session_principal_id(request),
                    action="route.structure.update",
                    entity_type="route",
                    entity_id=str(route.id),
                    metadata={"day_breaks": breaks, "parking_overrides": overrides},
                    ip=request.client.host if request.client else None,
                )
                await session.commit()
                if result is None:
                    Flash.error(request, "Сохранено, но маршрут не построился: линия прямая.")
                else:
                    Flash.success(request, "Сохранено, маршрут пересобран.")
                return RedirectResponse(
                    f"{request.url.path}?route_id={route.id}",
                    status_code=303,
                )
            context = await self._context(session, route, stops)
        return await self.templates.TemplateResponse(
            request, "sqladmin/route_structure.html", context
        )

    async def _context(self, session: Any, route: Route | None, stops: list[Any]) -> dict[str, Any]:
        if route is None:
            return {"route": None}
        days = list(
            await session.scalars(
                select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
            )
        )
        segments = list(
            await session.scalars(
                select(RouteSegment)
                .where(RouteSegment.route_id == route.id)
                .order_by(RouteSegment.leg_index, RouteSegment.seq)
            )
        )
        day_of = {}
        for day in days:
            day_of[day.last_stop_id] = day
        breaks = set(route.day_breaks or [])
        overrides = route.parking_overrides or {}
        rows = []
        for index, (stop_id, place_id, name) in enumerate(stops):
            leg = [s for s in segments if s.leg_index == index - 1]
            parking = overrides.get(str(place_id))
            rows.append(
                {
                    "position": index + 1,
                    "place_id": str(place_id),
                    "name": name,
                    "last": index == len(stops) - 1,
                    "ends_day": str(place_id) in breaks,
                    "parking": f"{parking[1]}, {parking[0]}" if parking else "",
                    "leg": " · ".join(
                        f"{_MODES.get(s.mode, s.mode)} "
                        f"{(s.distance_meters or 0) / 1000:.1f} км{_ROLES.get(s.role, '')}"
                        + (" (прямая)" if s.origin == "synthetic" else "")
                        for s in leg
                    ),
                    "day": day_of.get(stop_id),
                }
            )
        return {
            "route": route,
            "rows": rows,
            "days": days,
            "manual": route.days_manual,
        }
