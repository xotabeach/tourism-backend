"""Public transport lines and their timetables (spec 12b, section 2)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import UUID

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from sqlalchemy import func, or_, select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.presentation.auth import (
    require_admin_role,
    session_principal_id,
)
from tourism_backend.modules.route_builder.infrastructure.ai_factory import (
    get_ai_planning_provider,
)
from tourism_backend.modules.transit.application.schedule_draft import (
    MAX_SOURCE_CHARS,
    TextCompleter,
    draft_schedule,
)
from tourism_backend.modules.transit.application.schedules import (
    DEFAULT_SPEED_KMH,
    WEEKDAYS,
    ScheduleError,
    days_label,
    format_times,
    is_stale,
    parse_schedule_form,
)
from tourism_backend.modules.transit.infrastructure.models import (
    TransitLine,
    TransitSchedule,
    TransitVariant,
    TransitVariantStop,
)

_logger = logging.getLogger("tourism_backend.admin.transit")

KIND_LABELS = {
    "bus": "Автобус",
    "trolleybus": "Троллейбус",
    "tram": "Трамвай",
    "train": "Поезд",
    "share_taxi": "Маршрутка",
    "ferry": "Паром",
    "cable_car": "Канатная дорога",
}
STATUS_LABELS = {"active": "работает", "suspended": "приостановлена", "missing": "нет в OSM"}
_PAGE_SIZE = 100


def _today() -> Any:
    return datetime.now(UTC).date()


def schedule_form(schedule: TransitSchedule) -> dict[str, str]:
    """Stored period back into form values, for editing."""
    form = {
        "title": schedule.title,
        "date_from": schedule.date_from.isoformat() if schedule.date_from else "",
        "date_to": schedule.date_to.isoformat() if schedule.date_to else "",
        "first_departure": schedule.first_departure.strftime("%H:%M"),
        "last_departure": schedule.last_departure.strftime("%H:%M"),
        "headway_minutes": str(schedule.headway_minutes or ""),
        "departures": format_times(schedule.departures),
        "source": schedule.source,
        "checked_at": schedule.checked_at.isoformat(),
        "note": schedule.note or "",
    }
    for index in range(len(WEEKDAYS)):
        if schedule.days & (1 << index):
            form[f"day_{index}"] = "on"
    return form


class TransitAdmin(BaseView):
    name = "Линии и расписания"
    category = "Транспорт"
    icon = "fa-solid fa-bus"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)

    @expose("/transit", methods=["GET"], identity="transit")
    async def lines(self, request: Request) -> Response:
        if not require_admin_role(request):
            return Response(status_code=403)
        params = request.query_params
        kind = params.get("kind", "")
        status = params.get("status", "")
        query = params.get("q", "").strip()
        attention = params.get("attention", "")
        last_check = (
            select(
                TransitSchedule.line_id,
                func.max(TransitSchedule.checked_at).label("checked_at"),
                func.count().label("periods"),
            )
            .group_by(TransitSchedule.line_id)
            .subquery()
        )
        statement = (
            select(TransitLine, last_check.c.checked_at, last_check.c.periods)
            .outerjoin(last_check, last_check.c.line_id == TransitLine.id)
            .order_by(TransitLine.kind, TransitLine.ref, TransitLine.name)
        )
        if kind in KIND_LABELS:
            statement = statement.where(TransitLine.kind == kind)
        if status in STATUS_LABELS:
            statement = statement.where(TransitLine.status == status)
        if query:
            like = f"%{query}%"
            statement = statement.where(
                or_(
                    TransitLine.ref.ilike(like),
                    TransitLine.name.ilike(like),
                    TransitLine.operator.ilike(like),
                )
            )
        if attention == "mapping":
            statement = statement.where(TransitLine.needs_mapping.is_(True))
        elif attention == "no_schedule":
            statement = statement.where(last_check.c.periods.is_(None))
        async with self.session_maker() as session:
            rows = (await session.execute(statement)).all()
        today = _today()
        lines = [
            {
                "line": line,
                "kind": KIND_LABELS.get(line.kind, line.kind),
                "status": STATUS_LABELS.get(line.status, line.status),
                "periods": periods or 0,
                "checked_at": checked_at,
                "stale": periods is not None and is_stale(checked_at, today=today),
            }
            for line, checked_at, periods in rows
        ]
        if attention == "stale":
            lines = [row for row in lines if row["stale"]]
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/transit_lines.html",
            {
                "lines": lines[:_PAGE_SIZE],
                "total": len(lines),
                "kinds": KIND_LABELS,
                "statuses": STATUS_LABELS,
                "filters": {"kind": kind, "status": status, "q": query, "attention": attention},
            },
        )


class TransitLineAdmin(BaseView):
    """One line: status, speed, directions and timetable periods; opened
    from the list, so it has no menu entry of its own."""

    name = "Линия транспорта"
    category = "Транспорт"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/transit/line", methods=["GET", "POST"], identity="transit-line")
    async def line(self, request: Request) -> Response:
        if not require_admin_role(request):
            return Response(status_code=403)
        try:
            line_id = UUID(request.query_params.get("id", ""))
        except ValueError:
            Flash.error(request, "Линия не найдена.")
            return RedirectResponse(str(request.url_for("admin:view-transit")), status_code=303)
        here = f"{request.url.path}?id={line_id}"
        draft_forms: list[dict[str, str]] | None = None
        editing: TransitSchedule | None = None
        async with self.session_maker(expire_on_commit=False) as session:
            line = await session.get(TransitLine, line_id)
            if line is None:
                Flash.error(request, "Линия не найдена.")
                return RedirectResponse(str(request.url_for("admin:view-transit")), status_code=303)
            if request.method == "POST":
                form = {key: str(value) for key, value in (await request.form()).items()}
                action = form.get("action", "")
                if action == "line":
                    return await self._save_line(request, session, line, form, here)
                if action == "schedule":
                    return await self._save_schedule(request, session, line, form, here)
                if action == "delete":
                    return await self._delete_schedule(request, session, line, form, here)
                if action == "draft":
                    draft_forms = await self._draft(request, line, form)
            raw_edit = request.query_params.get("edit")
            if raw_edit:
                try:
                    editing = await session.get(TransitSchedule, UUID(raw_edit))
                except ValueError:
                    editing = None
                if editing is not None and editing.line_id != line.id:
                    editing = None
            context = await self._line_context(session, line)
        today = _today()
        if draft_forms is not None:
            forms = [{**form, "checked_at": today.isoformat()} for form in draft_forms]
        elif editing is not None:
            forms = [schedule_form(editing)]
        else:
            forms = [{"checked_at": today.isoformat()}]
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/transit_line.html",
            {
                **context,
                "forms": forms,
                "editing": editing,
                "drafted": draft_forms is not None,
                "weekdays": list(enumerate(WEEKDAYS)),
                "max_source_chars": MAX_SOURCE_CHARS,
            },
        )

    async def _line_context(self, session: Any, line: TransitLine) -> dict[str, Any]:
        stop_counts = (
            select(TransitVariantStop.variant_id, func.count().label("stops"))
            .group_by(TransitVariantStop.variant_id)
            .subquery()
        )
        variants = (
            await session.execute(
                select(TransitVariant, stop_counts.c.stops)
                .outerjoin(stop_counts, stop_counts.c.variant_id == TransitVariant.id)
                .where(TransitVariant.line_id == line.id)
                .order_by(TransitVariant.osm_id)
            )
        ).all()
        schedules = list(
            await session.scalars(
                select(TransitSchedule)
                .where(TransitSchedule.line_id == line.id)
                .order_by(TransitSchedule.created_at)
            )
        )
        today = _today()
        return {
            "line": line,
            "kind": KIND_LABELS.get(line.kind, line.kind),
            "status": STATUS_LABELS.get(line.status, line.status),
            "default_speed": DEFAULT_SPEED_KMH.get(line.kind),
            "variants": [{"variant": variant, "stops": stops or 0} for variant, stops in variants],
            "schedules": [
                {
                    "schedule": schedule,
                    "days": days_label(schedule.days),
                    "departures": format_times(schedule.departures),
                    "stale": is_stale(schedule.checked_at, today=today),
                }
                for schedule in schedules
            ],
        }

    async def _save_line(
        self, request: Request, session: Any, line: TransitLine, form: dict[str, str], here: str
    ) -> Response:
        status = form.get("status", line.status)
        reason = form.get("suspend_reason", "").strip() or None
        if status not in ("active", "suspended"):
            status = line.status
        if status == "suspended" and not reason:
            Flash.error(request, "Укажите причину приостановки.")
            return RedirectResponse(here, status_code=303)
        raw_speed = form.get("speed_kmh", "").strip()
        speed: int | None = None
        if raw_speed:
            try:
                speed = int(raw_speed)
            except ValueError:
                speed = 0
            if not 3 <= speed <= 150:
                Flash.error(request, "Скорость: целое число от 3 до 150 км/ч.")
                return RedirectResponse(here, status_code=303)
        before = {"status": line.status, "reason": line.suspend_reason, "speed": line.speed_kmh}
        # A line gone from OSM stays marked until it comes back in an import.
        if line.status != "missing":
            line.status = status
        line.suspend_reason = reason if status == "suspended" else None
        line.speed_kmh = speed
        await record_audit(
            session,
            actor_id=session_principal_id(request),
            action="transit.line.update",
            entity_type="transit_line",
            entity_id=str(line.id),
            metadata={
                "before": before,
                "after": {"status": line.status, "reason": line.suspend_reason, "speed": speed},
            },
            ip=request.client.host if request.client else None,
        )
        await session.commit()
        Flash.success(request, "Линия сохранена.")
        return RedirectResponse(here, status_code=303)

    async def _save_schedule(
        self, request: Request, session: Any, line: TransitLine, form: dict[str, str], here: str
    ) -> Response:
        schedule: TransitSchedule | None = None
        if form.get("schedule_id"):
            try:
                schedule = await session.get(TransitSchedule, UUID(form["schedule_id"]))
            except ValueError:
                schedule = None
            if schedule is None or schedule.line_id != line.id:
                Flash.error(request, "Период не найден.")
                return RedirectResponse(here, status_code=303)
        try:
            data = parse_schedule_form(form, today=_today())
        except ScheduleError as exc:
            Flash.error(request, str(exc))
            back = f"{here}&edit={schedule.id}" if schedule else here
            return RedirectResponse(back, status_code=303)
        created = schedule is None
        if schedule is None:
            schedule = TransitSchedule(line_id=line.id)
            session.add(schedule)
        schedule.title = data.title
        schedule.days = data.days
        schedule.date_from = data.date_from
        schedule.date_to = data.date_to
        schedule.first_departure = data.first_departure
        schedule.last_departure = data.last_departure
        schedule.headway_minutes = data.headway_minutes
        schedule.departures = data.departures
        schedule.source = data.source
        schedule.checked_at = data.checked_at
        schedule.note = data.note
        principal = session_principal_id(request)
        schedule.updated_by = str(principal) if principal else None
        await session.flush()
        await record_audit(
            session,
            actor_id=principal,
            action="transit.schedule.create" if created else "transit.schedule.update",
            entity_type="transit_schedule",
            entity_id=str(schedule.id),
            metadata={
                "line_id": str(line.id),
                "title": data.title,
                "days": data.days,
                "headway_minutes": data.headway_minutes,
                "departures": format_times(data.departures),
                "first": data.first_departure.isoformat(),
                "last": data.last_departure.isoformat(),
                "source": data.source,
                "checked_at": data.checked_at.isoformat(),
            },
            ip=request.client.host if request.client else None,
        )
        await session.commit()
        Flash.success(request, "Период сохранён.")
        return RedirectResponse(here, status_code=303)

    async def _delete_schedule(
        self, request: Request, session: Any, line: TransitLine, form: dict[str, str], here: str
    ) -> Response:
        try:
            schedule = await session.get(TransitSchedule, UUID(form.get("schedule_id", "")))
        except ValueError:
            schedule = None
        if schedule is None or schedule.line_id != line.id:
            Flash.error(request, "Период не найден.")
            return RedirectResponse(here, status_code=303)
        await record_audit(
            session,
            actor_id=session_principal_id(request),
            action="transit.schedule.delete",
            entity_type="transit_schedule",
            entity_id=str(schedule.id),
            metadata={"line_id": str(line.id), "form": schedule_form(schedule)},
            ip=request.client.host if request.client else None,
        )
        await session.delete(schedule)
        await session.commit()
        Flash.success(request, "Период удалён.")
        return RedirectResponse(here, status_code=303)

    async def _draft(
        self, request: Request, line: TransitLine, form: dict[str, str]
    ) -> list[dict[str, str]] | None:
        text = form.get("source_text", "").strip()
        if not text:
            Flash.error(request, "Вставьте текст расписания.")
            return None
        settings = request.app.state.settings
        try:
            provider = get_ai_planning_provider(settings) if settings.ai_planning_enabled else None
        except RuntimeError:
            provider = None
        if provider is None or not hasattr(provider, "complete_text"):
            Flash.error(request, "ИИ-помощник недоступен: не настроен провайдер.")
            return None
        try:
            forms, warning = await draft_schedule(
                cast(TextCompleter, provider),
                line_name=f"{KIND_LABELS.get(line.kind, line.kind)} {line.ref or ''} {line.name}",
                direction=form.get("direction", "").strip() or None,
                text=text,
            )
        except (ValueError, OSError) as exc:
            _logger.warning("transit_schedule_draft_failed", extra={"error": str(exc)})
            Flash.error(request, "ИИ не смог разобрать текст, заполните форму вручную.")
            return None
        if not forms:
            Flash.error(request, "ИИ не нашёл в тексте расписания.")
            return None
        note = "Проверьте поля и укажите источник, ничего не сохранено."
        Flash.success(request, f"Черновик готов: периодов {len(forms)}. {note}")
        if warning:
            Flash.error(request, f"ИИ отметил: {warning}")
        return forms
