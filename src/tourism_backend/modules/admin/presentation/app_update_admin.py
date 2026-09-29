"""Admin page for the outdated-build prompt and block (BACKEND-5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from tourism_backend.modules.app_update.application.policy import (
    ALL_KEYS,
    DEFAULT_DOWNLOAD_URL,
    DEFAULT_HARD_AFTER_DAYS,
    KEY_DOWNLOAD_URL,
    KEY_HARD_AFTER_DAYS,
    KEY_HARD_ENABLED,
    KEY_MESSAGE,
    KEY_MIN_SUPPORTED_BUILD,
    KEY_PREFIX,
    KEY_STORE_URL,
    ReleaseReader,
    parse_settings,
    validate_setting,
)
from tourism_backend.modules.app_update.presentation.router import DEFAULT_MESSAGE
from tourism_backend.modules.runtime_config.application.service import set_runtime_setting
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        KEY_HARD_AFTER_DAYS,
        "Дней до обязательного обновления",
        "Столько суток после публикации новой версии старая показывает мягкое окно "
        "«Обновить / Позже», потом экран без выхода. 0 означает блок сразу.",
        str(DEFAULT_HARD_AFTER_DAYS),
    ),
    (
        KEY_MIN_SUPPORTED_BUILD,
        "Минимальная сборка",
        "Сборки с номером меньше этого блокируются сразу, без отсрочки. Нужно только "
        "при несовместимом изменении API. Пусто означает не использовать.",
        "",
    ),
    (
        KEY_DOWNLOAD_URL,
        "Ссылка на скачивание",
        "Куда ведёт кнопка «Обновить», пока приложения нет в сторе.",
        DEFAULT_DOWNLOAD_URL,
    ),
    (
        KEY_STORE_URL,
        "Ссылка на стор (Android)",
        "Когда появится страница в магазине приложений, кнопка поведёт туда. "
        "Релиз для этого не нужен.",
        "",
    ),
    (KEY_MESSAGE, "Текст сообщения", "Показывается в окне и на экране блока.", DEFAULT_MESSAGE),
)


class AppUpdateConfigAdmin(BaseView):
    name = "Обновление приложения"
    category = "Настройки приложения"
    category_icon = "fa-solid fa-sliders"
    icon = "fa-solid fa-mobile-screen-button"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "settings.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/config/app-update", methods=["GET"], identity="config-app-update")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "settings.read"):
            Flash.error(request, "Недостаточно прав.")
            return RedirectResponse(request.url_for("admin:index"), status_code=303)
        async with self.session_maker(expire_on_commit=False) as session:
            stored = dict(
                (
                    await session.execute(
                        select(RuntimeSetting.key, RuntimeSetting.value).where(
                            RuntimeSetting.key.like(f"{KEY_PREFIX}%")
                        )
                    )
                )
                .tuples()
                .all()
            )
        effective = parse_settings(stored)
        media_dir = getattr(request.app.state, "media_dir", None)
        release = ReleaseReader(Path(media_dir) / "app").current() if media_dir else None
        hard_at = None
        if release is not None and effective.hard_enabled:
            hard_at = release.published_at + timedelta(days=effective.hard_after_days)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/app_update_config.html",
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
                "hard_enabled": effective.hard_enabled,
                "release": release,
                "hard_at": hard_at,
                "now": datetime.now(UTC),
                "can_write": require_permission(request, "settings.write"),
            },
        )

    @expose("/config/app-update/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        redirect_url = request.url_for("admin:view-config-app-update")
        if not require_permission(request, "settings.write"):
            Flash.error(request, "Недостаточно прав.")
            return RedirectResponse(redirect_url, status_code=303)
        form = await request.form()
        cleaned: dict[str, str] = {
            KEY_HARD_ENABLED: "on" if form.get(KEY_HARD_ENABLED) == "on" else "off"
        }
        labels = {key: label for key, label, _, _ in _FIELDS}
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
        async with self.session_maker(expire_on_commit=False) as session:
            existing = dict(
                (
                    await session.execute(
                        select(RuntimeSetting.key, RuntimeSetting.value).where(
                            RuntimeSetting.key.like(f"{KEY_PREFIX}%")
                        )
                    )
                )
                .tuples()
                .all()
            )
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
                action="runtime_config.app_update.update",
                entity_type="runtime_setting",
                entity_id="app_update",
                metadata={"changes": changes},
                ip=request.client.host if request.client else None,
                commit=True,
            )
        Flash.success(request, "Настройки обновления сохранены и применятся сразу.")
        return RedirectResponse(redirect_url, status_code=303)
