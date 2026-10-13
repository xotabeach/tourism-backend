"""Admin page for the «горный маршрут» thresholds (BACKEND-39)."""

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
from tourism_backend.modules.routes.application.mountain import (
    ALL_KEYS,
    KEY_MIN_ASCENT,
    KEY_MIN_LEVEL,
    KEY_PREFIX,
    MountainRules,
    validate_setting,
)
from tourism_backend.modules.runtime_config.application.service import set_runtime_setting
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_DEFAULTS = MountainRules()
_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        KEY_MIN_LEVEL,
        "Сложность, с которой маршрут горный",
        "Уровень сложности от 2 до 5. Маршрут с этой сложностью и выше считается горным.",
        str(_DEFAULTS.min_level),
    ),
    (
        KEY_MIN_ASCENT,
        "Набор высоты, м",
        "Суммарный подъём по маршруту, от 100 до 3000 м. С этого набора маршрут горный.",
        str(_DEFAULTS.min_ascent_m),
    ),
)


class MountainConfigAdmin(BaseView):
    name = "Горные маршруты"
    category = "Настройки приложения"
    category_icon = "fa-solid fa-sliders"
    icon = "fa-solid fa-mountain"
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

    @expose("/config/mountain", methods=["GET"], identity="config-mountain")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "settings.read"):
            Flash.error(request, "Недостаточно прав.")
            return RedirectResponse(request.url_for("admin:index"), status_code=303)
        stored = await self._stored()
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/mountain_config.html",
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

    @expose("/config/mountain/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        redirect_url = request.url_for("admin:view-config-mountain")
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
                action="runtime_config.mountain.update",
                entity_type="runtime_setting",
                entity_id="mountain",
                metadata={"changes": changes},
                ip=request.client.host if request.client else None,
                commit=True,
            )
        Flash.success(request, "Пороги горного маршрута сохранены и применятся сразу.")
        return RedirectResponse(redirect_url, status_code=303)
