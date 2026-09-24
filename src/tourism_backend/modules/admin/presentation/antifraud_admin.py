"""Admin views for route anti-fraud: hold queue, user state, violations, thresholds.

Everything an operator does here goes through ``antifraud_actions`` (which
takes the same user-row lock as the API) and is written to ``admin_audit_events``.
Editing thresholds and the "trusted" flag are admin-only; reviewing holds and
lifting blocks is open to ops.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from sqladmin import BaseView, action, expose
from sqladmin.filters import AllUniqueStringValuesFilter, OperationColumnFilter
from sqladmin.flash import Flash
from sqlalchemy import delete, func, select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.api.errors import AppError
from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.presentation.auth import (
    require_permission,
    session_principal_id,
)
from tourism_backend.modules.admin.presentation.datetime_fmt import ADMIN_COLUMN_TYPE_FORMATTERS
from tourism_backend.modules.admin.presentation.formatters import (
    format_fraud_flag,
    format_hold_status,
    format_user_fk,
    format_violation_kind,
    format_violation_mode,
)
from tourism_backend.modules.admin.presentation.permissions import (
    PermissionedModelView as ModelView,
)
from tourism_backend.modules.route_execution.application import antifraud_actions
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    ALL_KEYS,
    KEY_PREFIX,
    describe_settings,
    load_settings,
    validate_setting,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RoutePaceViolation,
    RoutePointsHold,
    UserFraudState,
)
from tourism_backend.modules.runtime_config.application.service import set_runtime_setting
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

ANTIFRAUD_CATEGORY = "Анти-фрод"
ANTIFRAUD_ICON = "fa-solid fa-shield-halved"

#: user actions available to ops, and the ones that need the admin role
_USER_ACTIONS = ("lift_block", "reset_flag", "block_24h", "block_7d")
_ADMIN_ONLY_ACTIONS = ("trust", "untrust")

#: Russian names for the raw enum stored in runtime_config / RoutePaceViolation.mode
#: (BACKEND-4: the screen showed "shadow"/"enforce" verbatim).
MODE_LABELS = {"off": "Выключено", "shadow": "Наблюдение", "enforce": "Боевой"}
#: Russian names for choice settings other than the mode.
CHOICE_LABELS = {"straight_line": "По прямой", "provider": "По участкам маршрута"}

_LABELS = {
    "af_mode": (
        "Режим",
        "Выключено — ничего не проверяет. Наблюдение — считает нарушения, "
        "но не ограничивает. Боевой — держит очки и блокирует запуск.",
    ),
    "af_pace_source": (
        "Оценка участка для темпа",
        "По прямой — как до перехода на OSM. По участкам маршрута — реальные тропы "
        "и дороги; пока включено «по прямой», участки сравниваются в журнале.",
    ),
    "af_pace_violation_ratio": ("Доля расчётного времени", "Участок быстрее этой доли — нарушение"),
    "af_min_mark_ratio": ("Пол отметки, доля", "Быстрее — отметка не даёт очков за точку"),
    "af_pace_leg_min_estimate_seconds": (
        "Мин. оценка участка, с",
        "Короче — участок не оценивается",
    ),
    "af_batch_window_seconds": ("Окно пачки, с", "Нарушения в окне считаются одним"),
    "af_min_mark_gap_seconds": ("Мин. пауза между отметками, с", "Ниже — нет очков за точку"),
    "af_flag_violations": ("Нарушений для флага", "Очки уходят на проверку"),
    "af_flag_window_hours": ("Окно флага, ч", ""),
    "af_block_violations": ("Нарушений для блокировки", "Не меньше порога флага"),
    "af_block_window_hours": ("Окно блокировки, ч", ""),
    "af_block_ladder_hours": (
        "Лестница блокировки, ч",
        "Через запятую, до 3 ступеней, например 1,24,168",
    ),
    "af_ladder_memory_days": ("Память лестницы, дн", "Повторы в этот срок повышают ступень"),
    "af_route_points_cooldown_days": ("Кулдаун очков за маршрут, дн", "0 — выключен"),
    "af_daily_points_cap": ("Дневной потолок очков", "Сутки по Москве"),
    "af_gps_tolerance_meters": ("Допуск GPS, м", "Радиус поиска ближайшей точки"),
    "af_gps_min_accuracy_meters": ("Мин. точность GPS, м", "Хуже — позиция не учитывается"),
    "af_hold_overdue_days": ("Просрочка холда, дн", "Старше — попадает в счётчик просроченных"),
}


def _pks(request: Request) -> list[UUID]:
    found: list[UUID] = []
    for raw in request.query_params.get("pks", "").split(","):
        with contextlib.suppress(ValueError):
            found.append(UUID(raw.strip()))
    return found


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def apply_user_fraud_action(
    request: Request,
    session_maker: Any,
    *,
    kind: str,
    identity: str,
) -> Response:
    """Shared by the user-state view and the users view (manual block, trust...)."""

    back = RedirectResponse(str(request.url_for("admin:list", identity=identity)), status_code=303)
    actor_id = session_principal_id(request)
    if actor_id is None:
        return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
    if not require_permission(request, "antifraud.write"):
        Flash.error(request, "Недостаточно прав.")
        return back
    if kind in _ADMIN_ONLY_ACTIONS and not require_permission(request, "antifraud.trust"):
        Flash.error(request, "Недостаточно прав для доверенного статуса.")
        return back
    if kind not in _USER_ACTIONS and kind not in _ADMIN_ONLY_ACTIONS:
        return back

    done = 0
    for user_id in _pks(request):
        async with session_maker(expire_on_commit=False) as session:
            try:
                if kind == "lift_block":
                    await antifraud_actions.lift_block(session, user_id=user_id)
                elif kind == "reset_flag":
                    await antifraud_actions.reset_flag(session, user_id=user_id)
                elif kind == "block_24h":
                    await antifraud_actions.block_manually(session, user_id=user_id, hours=24)
                elif kind == "block_7d":
                    await antifraud_actions.block_manually(session, user_id=user_id, hours=168)
                elif kind == "trust":
                    await antifraud_actions.set_trusted(session, user_id=user_id, trusted=True)
                else:
                    await antifraud_actions.set_trusted(session, user_id=user_id, trusted=False)
            except AppError as exc:
                Flash.error(request, exc.message)
                continue
            await record_audit(
                session,
                actor_id=actor_id,
                action=f"antifraud.user.{kind}",
                entity_type="user",
                entity_id=str(user_id),
                ip=_ip(request),
                commit=True,
            )
            done += 1
    if done:
        Flash.success(request, f"Готово: {done}.")
    return back


class RoutePointsHoldAdmin(ModelView, model=RoutePointsHold):
    """Очередь очков, ожидающих решения оператора."""

    category = ANTIFRAUD_CATEGORY
    category_icon = ANTIFRAUD_ICON
    name = "Холд очков"
    name_plural = "Очки на проверке"
    icon = "fa-solid fa-hourglass-half"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RoutePointsHold.status,
        RoutePointsHold.user_id,
        RoutePointsHold.execution_id,
        RoutePointsHold.amount,
        RoutePointsHold.deducted_points,
        RoutePointsHold.reason,
        RoutePointsHold.created_at,
        RoutePointsHold.decided_at,
    ]
    column_labels = {
        RoutePointsHold.status: "Статус",
        RoutePointsHold.user_id: "Пользователь",
        RoutePointsHold.execution_id: "Прохождение",
        RoutePointsHold.amount: "Очков за маршрут",
        RoutePointsHold.deducted_points: "Снято с баланса",
        RoutePointsHold.reason: "Причина",
        RoutePointsHold.created_at: "Создан",
        RoutePointsHold.decided_at: "Решён",
        RoutePointsHold.decided_by: "Решил",
        RoutePointsHold.note: "Заметка",
    }
    column_formatters = {
        RoutePointsHold.status: format_hold_status,
        RoutePointsHold.user_id: format_user_fk,
    }
    column_formatters_detail = column_formatters
    column_sortable_list = [
        RoutePointsHold.status,
        RoutePointsHold.created_at,
        RoutePointsHold.amount,
    ]
    column_default_sort = (RoutePointsHold.created_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(RoutePointsHold.status),
        OperationColumnFilter(RoutePointsHold.user_id, title="ID пользователя"),
    ]
    # Decisions go through actions so the balance, the run, the audit trail and
    # the notification stay consistent; a raw form edit would break all four.
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50

    async def _decide(self, request: Request, *, approve: bool) -> Response:
        back = RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)), status_code=303
        )
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        done = 0
        for hold_id in _pks(request):
            async with self.session_maker(expire_on_commit=False) as session:
                try:
                    hold = await antifraud_actions.decide_hold(
                        session, hold_id=hold_id, approve=approve, principal_id=actor_id
                    )
                except AppError as exc:
                    Flash.error(request, exc.message)
                    continue
                await record_audit(
                    session,
                    actor_id=actor_id,
                    action="antifraud.hold.approve" if approve else "antifraud.hold.reject",
                    entity_type="route_points_hold",
                    entity_id=str(hold_id),
                    metadata={
                        "user_id": str(hold.user_id),
                        "amount": hold.amount,
                        "final_status": hold.status,
                    },
                    ip=_ip(request),
                    commit=True,
                )
                done += 1
        if done:
            Flash.success(request, f"Решений: {done}.")
        return back

    @action(
        name="approve_hold",
        label="Подтвердить очки",
        confirmation_message="Вернуть пользователю очки за выбранные прохождения?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def approve_hold(self, request: Request) -> Response:
        return await self._decide(request, approve=True)

    @action(
        name="reject_hold",
        label="Отклонить очки",
        confirmation_message="Не начислять очки за выбранные прохождения?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reject_hold(self, request: Request) -> Response:
        return await self._decide(request, approve=False)


class UserFraudStateAdmin(ModelView, model=UserFraudState):
    """Кто отмечен, заблокирован или доверенный."""

    category = ANTIFRAUD_CATEGORY
    category_icon = ANTIFRAUD_ICON
    name = "Состояние пользователя"
    name_plural = "Отмеченные пользователи"
    icon = "fa-solid fa-user-shield"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        UserFraudState.user_id,
        UserFraudState.is_flagged,
        UserFraudState.blocked_until,
        UserFraudState.ladder_level,
        UserFraudState.last_offence_at,
        UserFraudState.is_trusted,
        UserFraudState.updated_at,
    ]
    column_labels = {
        UserFraudState.user_id: "Пользователь",
        UserFraudState.is_flagged: "Отмечен",
        UserFraudState.flagged_at: "Отмечен с",
        UserFraudState.blocked_until: "Блок до",
        UserFraudState.ladder_level: "Ступень",
        UserFraudState.last_offence_at: "Последнее нарушение",
        UserFraudState.counters_from: "Счётчики с",
        UserFraudState.is_trusted: "Доверенный",
        UserFraudState.updated_at: "Обновлено",
    }
    column_formatters = {
        UserFraudState.user_id: format_user_fk,
        UserFraudState.is_flagged: format_fraud_flag,
        UserFraudState.is_trusted: format_fraud_flag,
    }
    column_formatters_detail = column_formatters
    column_sortable_list = [UserFraudState.blocked_until, UserFraudState.updated_at]
    column_default_sort = (UserFraudState.updated_at, True)
    column_filters: ClassVar[list[Any]] = [
        OperationColumnFilter(UserFraudState.user_id, title="ID пользователя"),
    ]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50

    @action(
        name="lift_block",
        label="Снять блокировку",
        confirmation_message="Снять блокировку запуска маршрутов?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def lift_block(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="lift_block", identity=self.identity
        )

    @action(
        name="reset_flag",
        label="Сбросить флаг и счётчики",
        confirmation_message="Снять отметку и начать подсчёт нарушений заново? "
        "Решения по холдам это не меняет.",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reset_flag(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="reset_flag", identity=self.identity
        )

    @action(
        name="block_24h",
        label="Заблокировать на 24 часа",
        confirmation_message="Запретить запуск новых маршрутов на 24 часа?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def block_24h(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="block_24h", identity=self.identity
        )

    @action(
        name="block_7d",
        label="Заблокировать на 7 дней",
        confirmation_message="Запретить запуск новых маршрутов на 7 дней?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def block_7d(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="block_7d", identity=self.identity
        )

    @action(
        name="trust",
        label="Сделать доверенным",
        confirmation_message="Исключить из проверки темпа? Кап и кулдаун продолжат действовать.",
        add_in_detail=True,
        add_in_list=True,
    )
    async def trust(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="trust", identity=self.identity
        )

    @action(
        name="untrust",
        label="Снять «доверенный»",
        confirmation_message="Вернуть проверку темпа для пользователя?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def untrust(self, request: Request) -> Response:
        return await apply_user_fraud_action(
            request, self.session_maker, kind="untrust", identity=self.identity
        )


class RoutePaceViolationAdmin(ModelView, model=RoutePaceViolation):
    """Журнал подозрительных отметок (хранится 90 дней)."""

    category = ANTIFRAUD_CATEGORY
    category_icon = ANTIFRAUD_ICON
    name = "Нарушение темпа"
    name_plural = "Нарушения темпа"
    icon = "fa-solid fa-gauge-high"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RoutePaceViolation.occurred_at,
        RoutePaceViolation.user_id,
        RoutePaceViolation.kind,
        RoutePaceViolation.estimate_seconds,
        RoutePaceViolation.actual_seconds,
        RoutePaceViolation.gps_verdict,
        RoutePaceViolation.offline_sync,
        RoutePaceViolation.counted,
        RoutePaceViolation.mode,
    ]
    column_labels = {
        RoutePaceViolation.occurred_at: "Когда",
        RoutePaceViolation.user_id: "Пользователь",
        RoutePaceViolation.execution_id: "Прохождение",
        RoutePaceViolation.stop_id: "Точка",
        RoutePaceViolation.kind: "Тип",
        RoutePaceViolation.estimate_seconds: "Расчётно, с",
        RoutePaceViolation.actual_seconds: "Фактически, с",
        RoutePaceViolation.gps_verdict: "GPS",
        RoutePaceViolation.gps_distance_bucket_m: "До точки, м (округл.)",
        RoutePaceViolation.timing_source: "Источник времени",
        RoutePaceViolation.offline_sync: "Офлайн",
        RoutePaceViolation.counted: "Засчитано",
        RoutePaceViolation.mode: "Режим",
        RoutePaceViolation.created_at: "Записано",
    }
    column_formatters = {
        RoutePaceViolation.user_id: format_user_fk,
        RoutePaceViolation.kind: format_violation_kind,
        RoutePaceViolation.mode: format_violation_mode,
    }
    column_formatters_detail = column_formatters
    column_sortable_list = [RoutePaceViolation.occurred_at]
    column_default_sort = (RoutePaceViolation.occurred_at, True)
    column_filters: ClassVar[list[Any]] = [
        OperationColumnFilter(RoutePaceViolation.user_id, title="ID пользователя"),
        AllUniqueStringValuesFilter(RoutePaceViolation.mode),
        AllUniqueStringValuesFilter(RoutePaceViolation.kind),
    ]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class AntiFraudConfigAdmin(BaseView):
    """Режим и пороги анти-фрода: меняются без деплоя, границы проверяются."""

    name = "Анти-фрод: пороги"
    category = ANTIFRAUD_CATEGORY
    category_icon = ANTIFRAUD_ICON
    icon = "fa-solid fa-sliders"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "antifraud.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/config/antifraud", methods=["GET"], identity="config-antifraud")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "antifraud.read"):
            Flash.error(request, "Доступно только роли admin.")
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
            effective = await load_settings(session)
            now = datetime.now(UTC)
            held = int(
                await session.scalar(
                    select(func.count())
                    .select_from(RoutePointsHold)
                    .where(RoutePointsHold.status == "held")
                )
                or 0
            )
            overdue = await antifraud_actions.count_overdue_holds(
                session, older_than_days=effective.hold_overdue_days, now=now
            )
            recent = dict(
                (
                    await session.execute(
                        select(RoutePaceViolation.mode, func.count())
                        .where(
                            RoutePaceViolation.counted.is_(True),
                            RoutePaceViolation.occurred_at >= now - timedelta(hours=24),
                        )
                        .group_by(RoutePaceViolation.mode)
                    )
                )
                .tuples()
                .all()
            )
            flagged = int(
                await session.scalar(
                    select(func.count())
                    .select_from(UserFraudState)
                    .where(UserFraudState.is_flagged.is_(True))
                )
                or 0
            )
            blocked = int(
                await session.scalar(
                    select(func.count())
                    .select_from(UserFraudState)
                    .where(UserFraudState.blocked_until > now)
                )
                or 0
            )
        fields = [
            {
                "key": item.key,
                "kind": item.kind,
                "label": _LABELS.get(item.key, (item.key, ""))[0],
                "hint": _LABELS.get(item.key, (item.key, ""))[1],
                "default": item.default,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "value": stored.get(item.key, ""),
                "options": item.options,
            }
            for item in describe_settings()
        ]
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/antifraud_config.html",
            context={
                "fields": fields,
                "effective_mode": effective.mode.value,
                "mode_labels": MODE_LABELS,
                "choice_labels": CHOICE_LABELS,
                "held": held,
                "overdue": overdue,
                "overdue_days": effective.hold_overdue_days,
                "violations_shadow": int(recent.get("shadow", 0)),
                "violations_enforce": int(recent.get("enforce", 0)),
                "flagged": flagged,
                "blocked": blocked,
            },
        )

    @expose("/config/antifraud/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        redirect_url = request.url_for("admin:view-config-antifraud")
        if not require_permission(request, "antifraud.write"):
            Flash.error(request, "Доступно только роли admin.")
            return RedirectResponse(redirect_url, status_code=303)
        form = await request.form()
        cleaned: dict[str, str] = {}
        for key in sorted(ALL_KEYS):
            raw = str(form.get(key) or "").strip()
            if not raw:
                continue  # empty means "use the default"
            try:
                cleaned[key] = validate_setting(key, raw)
            except ValueError as exc:
                label = _LABELS.get(key, (key, ""))[0]
                Flash.error(request, f"{label}: {exc}")
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
            for key in ALL_KEYS - cleaned.keys():
                if key in existing:
                    changes[key] = {"old": existing[key], "new": None}
                    await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
            await record_audit(
                session,
                actor_id=actor_id,
                action="antifraud.config.update",
                entity_type="runtime_setting",
                entity_id="antifraud",
                metadata={"changes": changes},
                ip=_ip(request),
                commit=True,
            )
        Flash.success(request, "Настройки анти-фрода сохранены и применятся сразу.")
        return RedirectResponse(redirect_url, status_code=303)
