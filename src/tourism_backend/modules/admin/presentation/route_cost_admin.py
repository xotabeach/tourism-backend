"""Admin page for the route cost rules (BACKEND-56, spec 33 D4/D12).

Saving recomputes every route's cost at once: the level of a route must not
depend on whether it was rebuilt after the rules changed.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from sqlalchemy import delete, select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.presentation.auth import (
    require_permission,
    session_principal_id,
)
from tourism_backend.modules.routes.application.route_cost import (
    ALL_KEYS,
    KEY_BUS_FARE,
    KEY_FUEL_PER_100KM,
    KEY_FUEL_PRICE,
    KEY_LEVEL_BOUNDS,
    KEY_NIGHT_PRICES,
    KEY_PREFIX,
    CostRules,
    load_rules,
    refresh_costs,
    validate_setting,
)
from tourism_backend.modules.runtime_config.application.service import set_runtime_setting
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_DEFAULTS = CostRules()
_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        KEY_LEVEL_BOUNDS,
        "Границы уровней, ₽ на человека в день",
        "Три числа через запятую: верх «Экономно», «Средне» и «Комфорт». Дороже последней "
        "границы маршрут «Без ограничений». Числа строго растут.",
        ",".join(map(str, _DEFAULTS.level_bounds)),
    ),
    (
        KEY_BUS_FARE,
        "Поездка на автобусе или троллейбусе, ₽",
        "Берётся для участка, у которого редактор не указал цену. От 10 до 1000.",
        str(_DEFAULTS.bus_fare),
    ),
    (
        KEY_FUEL_PER_100KM,
        "Расход топлива, л на 100 км",
        "Для строки «дорога на машину». От 3 до 30.",
        f"{_DEFAULTS.fuel_l_per_100km:g}",
    ),
    (
        KEY_FUEL_PRICE,
        "Цена литра топлива, ₽",
        "От 20 до 500.",
        str(_DEFAULTS.fuel_price),
    ),
    (
        KEY_NIGHT_PRICES,
        "Ночлег на человека по уровням, ₽",
        "Четыре числа через запятую, от «Экономно» до «Без ограничений», не убывают. "
        "В уровень маршрута не входят, показываются отдельной строкой.",
        ",".join(map(str, _DEFAULTS.night_prices)),
    ),
)


class CostConfigAdmin(BaseView):
    name = "Стоимость маршрутов"
    category = "Настройки приложения"
    category_icon = "fa-solid fa-sliders"
    icon = "fa-solid fa-ruble-sign"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "settings.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    async def _stored(self) -> dict[str, str]:
        async with self.session_maker(expire_on_commit=False) as session:
            rows = await session.execute(
                select(RuntimeSetting.key, RuntimeSetting.value).where(
                    RuntimeSetting.key.like(f"{KEY_PREFIX}%")
                )
            )
            return dict(rows.tuples().all())

    @expose("/config/route-cost", methods=["GET"], identity="config-route-cost")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "settings.read"):
            Flash.error(request, "Недостаточно прав.")
            return RedirectResponse(request.url_for("admin:index"), status_code=303)
        stored = await self._stored()
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/route_cost_config.html",
            context={
                "fields": [
                    {
                        "key": key,
                        "label": label,
                        "hint": hint,
                        "default": default,
                        "value": stored.get(key, ""),
                    }
                    for key, label, hint, default in _FIELDS
                ],
                "can_write": require_permission(request, "settings.write"),
            },
        )

    @expose("/config/route-cost/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        redirect_url = request.url_for("admin:view-config-route-cost")
        if not require_permission(request, "settings.write"):
            Flash.error(request, "Недостаточно прав.")
            return RedirectResponse(redirect_url, status_code=303)
        form = await request.form()
        labels = {key: label for key, label, _, _ in _FIELDS}
        cleaned: dict[str, str] = {}
        for key, *_ in _FIELDS:
            raw = str(form.get(key) or "").strip()
            if not raw:
                continue  # empty means "use the default"
            try:
                cleaned[key] = validate_setting(key, raw)
            except ValueError as exc:
                Flash.error(request, f"{labels[key]}: {exc}")
                return RedirectResponse(redirect_url, status_code=303)

        actor_id = session_principal_id(request)
        existing = await self._stored()
        async with self.session_maker(expire_on_commit=False) as session:
            changes: dict[str, dict[str, str | None]] = {}
            for key, value in cleaned.items():
                if existing.get(key) != value:
                    changes[key] = {"old": existing.get(key), "new": value}
                    await set_runtime_setting(
                        session,
                        key=key,
                        value=value,
                        updated_by_principal_id=actor_id,
                        commit=False,
                    )
            for key in set(ALL_KEYS) - cleaned.keys():
                if key in existing:
                    changes[key] = {"old": existing[key], "new": None}
                    await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
            await record_audit(
                session,
                actor_id=actor_id,
                action="runtime_config.route_cost.update",
                entity_type="runtime_setting",
                entity_id="route_cost",
                metadata={"changes": changes},
                ip=request.client.host if request.client else None,
            )
            await session.flush()
            recomputed = await refresh_costs(session, rules=await load_rules(session))
            await session.commit()
        Flash.success(request, f"Сохранено, стоимость пересчитана у маршрутов: {recomputed}.")
        return RedirectResponse(redirect_url, status_code=303)
