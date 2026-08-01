"""SQLAdmin ModelViews for ops (users, OTP, support, principals)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqladmin import ModelView, action, expose
from sqladmin.filters import AllUniqueStringValuesFilter, OperationColumnFilter
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from wtforms import PasswordField  # type: ignore[import-untyped]
from wtforms.validators import Length, Optional  # type: ignore[import-untyped]

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.application.passwords import hash_password
from tourism_backend.modules.admin.application.support_ops import operator_reply
from tourism_backend.modules.admin.infrastructure.models import (
    AdminAuditEvent,
    AdminPrincipal,
    AdminRoleBinding,
)
from tourism_backend.modules.admin.presentation.auth import (
    require_admin_role,
    session_principal_id,
)
from tourism_backend.modules.admin.presentation.filters import OtpLinkedUserIdFilter
from tourism_backend.modules.admin.presentation.formatters import (
    format_admin_role,
    format_debug_code,
    format_message_author,
    format_ticket_kind,
    format_ticket_status,
    format_user_avatar_name,
    format_user_cover,
    format_user_id_peek,
)
from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    validate_display_name,
)
from tourism_backend.modules.identity.application.schemas import normalize_ru_phone
from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    AuthPhoneChangeChallenge,
    User,
)
from tourism_backend.modules.media.application.service import resolve_urls
from tourism_backend.modules.support.infrastructure.models import SupportMessage, SupportTicket

_AUTHOR_RU = {
    "user": "Пользователь",
    "operator": "Оператор",
    "assistant": "Ассистент",
    "system": "Система",
}


class UserAdmin(ModelView, model=User):
    category = "Пользователи"
    category_icon = "fa-solid fa-user-group"
    name = "Пользователь"
    name_plural = "Пользователи"
    icon = "fa-solid fa-users"
    column_list = [
        User.id,
        User.display_name,
        User.phone_e164,
        User.travel_points,
        User.created_at,
        User.notify_push_enabled,
    ]
    column_labels = {
        User.id: "Баннер",
        User.display_name: "Профиль",
        User.phone_e164: "Телефон",
        User.travel_points: "ТП",
        User.created_at: "Создан",
        User.notify_push_enabled: "Push",
        User.notify_sms_enabled: "SMS",
        User.notify_haptics_enabled: "Тактильность",
    }
    column_formatters = {
        User.id: format_user_cover,
        User.display_name: format_user_avatar_name,
        User.notify_push_enabled: lambda m, _a: "Да" if m.notify_push_enabled else "Нет",
    }
    column_formatters_detail = {
        User.id: format_user_cover,
        User.display_name: format_user_avatar_name,
    }
    column_searchable_list = [User.display_name, User.phone_e164]
    column_sortable_list = [User.created_at, User.display_name, User.travel_points]
    column_default_sort = (User.created_at, True)
    column_filters = [
        OperationColumnFilter(User.phone_e164, title="Телефон"),
        OperationColumnFilter(User.id, title="ID пользователя"),
    ]
    form_columns = [
        User.display_name,
        User.phone_e164,
        User.travel_points,
        User.notify_push_enabled,
        User.notify_sms_enabled,
        User.notify_haptics_enabled,
    ]
    form_args = {
        "display_name": {
            "label": "Имя",
            "validators": [Length(min=DISPLAY_NAME_MIN_LENGTH, max=DISPLAY_NAME_MAX_LENGTH)],
        },
        "phone_e164": {"label": "Телефон"},
        "travel_points": {"label": "Путевые точки"},
        "notify_push_enabled": {"label": "Push"},
        "notify_sms_enabled": {"label": "SMS"},
        "notify_haptics_enabled": {"label": "Тактильность"},
    }
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def list(self, request: Request) -> Any:
        pagination = await super().list(request)
        ids = [row.id for row in pagination.rows]
        media: dict[UUID, dict[str, str | None]] = {
            uid: {"avatar": None, "cover": None} for uid in ids
        }
        if ids:
            async with self.session_maker(expire_on_commit=False) as session:
                avatars = await resolve_urls(
                    session, entity_type="user", entity_ids=ids, role="avatar"
                )
                covers = await resolve_urls(
                    session, entity_type="user", entity_ids=ids, role="cover"
                )
            for uid in ids:
                media[uid] = {
                    "avatar": avatars.get(uid),
                    "cover": covers.get(uid),
                }
        request.state.user_media = media
        return pagination

    async def get_object_for_details(self, request: Request) -> Any:
        model = await super().get_object_for_details(request)
        if model is not None:
            async with self.session_maker(expire_on_commit=False) as session:
                avatars = await resolve_urls(
                    session, entity_type="user", entity_ids=[model.id], role="avatar"
                )
                covers = await resolve_urls(
                    session, entity_type="user", entity_ids=[model.id], role="cover"
                )
            request.state.user_media = {
                model.id: {
                    "avatar": avatars.get(model.id),
                    "cover": covers.get(model.id),
                }
            }
        return model

    async def update_model(self, request: Request, pk: Any, data: dict[str, Any]) -> Any:
        actor_id = session_principal_id(request)
        try:
            display_name = validate_display_name(str(data.get("display_name") or ""))
        except ValueError as exc:
            raise AppError(
                code="validation_error",
                message=str(exc),
                status_code=400,
            ) from exc
        try:
            phone = normalize_ru_phone(str(data.get("phone_e164") or ""))
        except ValueError as exc:
            raise AppError(
                code="validation_error",
                message="Invalid phone",
                status_code=400,
            ) from exc
        if not phone.startswith("+7") or len(phone) != 12 or not phone[1:].isdigit():
            raise AppError(
                code="validation_error",
                message="phone must match +7XXXXXXXXXX",
                status_code=400,
            )
        try:
            points = int(data.get("travel_points") or 0)
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="validation_error",
                message="travel_points must be an integer",
                status_code=400,
            ) from exc
        if points < 0 or points > 1_000_000:
            raise AppError(
                code="validation_error",
                message="travel_points out of range",
                status_code=400,
            )

        async with self.session_maker(expire_on_commit=False) as session:
            user = await session.get(User, UUID(str(pk)))
            if user is None:
                raise AppError(code="not_found", message="User not found", status_code=404)
            clash = await session.execute(
                select(User.id).where(User.phone_e164 == phone, User.id != user.id).limit(1)
            )
            if clash.scalar_one_or_none() is not None:
                raise AppError(
                    code="conflict",
                    message="Phone already in use",
                    status_code=409,
                )
            user.display_name = display_name
            user.phone_e164 = phone
            user.travel_points = points
            user.notify_push_enabled = bool(data.get("notify_push_enabled"))
            user.notify_sms_enabled = bool(data.get("notify_sms_enabled"))
            user.notify_haptics_enabled = bool(data.get("notify_haptics_enabled"))
            await record_audit(
                session,
                actor_id=actor_id,
                action="admin.user_update",
                entity_type="user",
                entity_id=str(user.id),
                ip=request.client.host if request.client else None,
            )
            await session.commit()
            await session.refresh(user)
            return user


def _otp_columns(*, show_debug_code: bool) -> list[Any]:
    cols: list[Any] = [
        AuthOtpChallenge.id,
        AuthOtpChallenge.phone_e164,
        AuthOtpChallenge.display_name,
        AuthOtpChallenge.expires_at,
        AuthOtpChallenge.attempts,
        AuthOtpChallenge.consumed_at,
        AuthOtpChallenge.created_at,
    ]
    if show_debug_code:
        cols.insert(3, AuthOtpChallenge.debug_code)
    return cols


def _phone_change_columns(*, show_debug_code: bool) -> list[Any]:
    cols: list[Any] = [
        AuthPhoneChangeChallenge.id,
        AuthPhoneChangeChallenge.user_id,
        AuthPhoneChangeChallenge.phone_e164,
        AuthPhoneChangeChallenge.expires_at,
        AuthPhoneChangeChallenge.attempts,
        AuthPhoneChangeChallenge.consumed_at,
        AuthPhoneChangeChallenge.created_at,
    ]
    if show_debug_code:
        cols.insert(3, AuthPhoneChangeChallenge.debug_code)
    return cols


class SupportTicketAdmin(ModelView, model=SupportTicket):
    category = "Поддержка"
    category_icon = "fa-solid fa-headset"
    name = "Тикет"
    name_plural = "Тикеты"
    icon = "fa-solid fa-ticket"
    column_list = [
        SupportTicket.id,
        SupportTicket.user_id,
        SupportTicket.kind,
        SupportTicket.subject,
        SupportTicket.status,
        SupportTicket.created_at,
        SupportTicket.updated_at,
    ]
    column_labels = {
        SupportTicket.id: "ID",
        SupportTicket.user_id: "Пользователь",
        SupportTicket.kind: "Тип",
        SupportTicket.subject: "Тема",
        SupportTicket.status: "Статус",
        SupportTicket.created_at: "Создан",
        SupportTicket.updated_at: "Обновлён",
    }
    column_formatters = {
        SupportTicket.status: format_ticket_status,
        SupportTicket.kind: format_ticket_kind,
        SupportTicket.user_id: format_user_id_peek,
    }
    column_searchable_list = [SupportTicket.subject]
    column_filters = [AllUniqueStringValuesFilter(SupportTicket.status)]
    form_columns = [SupportTicket.status]
    can_create = False
    can_delete = False
    can_edit = True
    page_size = 50
    details_template = "sqladmin/support_chat.html"

    async def _load_chat_messages(self, ticket_id: UUID) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        async with self.session_maker(expire_on_commit=False) as session:
            result = await session.execute(
                select(SupportMessage)
                .where(SupportMessage.ticket_id == ticket_id)
                .order_by(SupportMessage.created_at.asc())
            )
            for msg in result.scalars().all():
                messages.append(
                    {
                        "id": str(msg.id),
                        "author": _AUTHOR_RU.get(msg.author, msg.author),
                        "author_key": msg.author,
                        "body": msg.body,
                        "created_at": msg.created_at.strftime("%d.%m.%Y %H:%M"),
                    }
                )
        return messages

    async def get_object_for_details(self, request: Request) -> Any:
        model = await super().get_object_for_details(request)
        if model is not None:
            request.state.support_chat_messages = await self._load_chat_messages(model.id)
        else:
            request.state.support_chat_messages = []
        return model

    @action(
        name="reply_as_operator",
        label="Ответить как оператор",
        confirmation_message="Открыть чат выбранного тикета?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reply_as_operator(self, request: Request) -> Response:
        pks = request.query_params.get("pks", "")
        ticket_id = pks.split(",")[0].strip() if pks else ""
        if ticket_id:
            return RedirectResponse(
                str(request.url_for("admin:details", identity="support-ticket", pk=ticket_id)),
                status_code=302,
            )
        return RedirectResponse(
            str(request.url_for("admin:list", identity="support-ticket")),
            status_code=302,
        )

    @expose("/messages/{pk}", methods=["GET"])
    async def list_messages(self, request: Request) -> Response:
        """JSON poll endpoint for live chat updates on the details page."""
        if session_principal_id(request) is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        pk = str(request.path_params.get("pk") or "").strip()
        try:
            ticket_id = UUID(pk)
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="validation_error",
                message="ticket_id must be a UUID",
                status_code=400,
            ) from exc

        async with self.session_maker(expire_on_commit=False) as session:
            ticket = await session.get(SupportTicket, ticket_id)
            if ticket is None:
                return JSONResponse({"detail": "not_found"}, status_code=404)
            ticket_status = ticket.status

        messages = await self._load_chat_messages(ticket_id)
        return JSONResponse(
            {
                "ticket_id": str(ticket_id),
                "status": ticket_status,
                "messages": messages,
            }
        )

    @expose("/reply/{pk}", methods=["POST"])
    async def post_reply(self, request: Request) -> Response:
        """Compose box on the chat details page — not SQLAdmin model create."""
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)

        pk = str(request.path_params.get("pk") or "").strip()
        try:
            ticket_id = UUID(pk)
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="validation_error",
                message="ticket_id must be a UUID",
                status_code=400,
            ) from exc

        form = await request.form()
        body = str(form.get("body") or "")
        client_ip = request.client.host if request.client else None
        details_url = str(
            request.url_for("admin:details", identity="support-ticket", pk=str(ticket_id))
        )
        try:
            async with self.session_maker(expire_on_commit=False) as session:
                await operator_reply(
                    session,
                    ticket_id=ticket_id,
                    body=body,
                    actor_id=actor_id,
                    ip=client_ip,
                )
        except AppError:
            # Stay in the chat thread; operator can retry after fixing body/status.
            return RedirectResponse(details_url, status_code=303)
        return RedirectResponse(details_url, status_code=303)


class SupportMessageAdmin(ModelView, model=SupportMessage):
    category = "Поддержка"
    category_icon = "fa-solid fa-headset"
    name = "Сообщение"
    name_plural = "Сообщения"
    icon = "fa-solid fa-comments"
    column_list = [
        SupportMessage.id,
        SupportMessage.ticket_id,
        SupportMessage.author,
        SupportMessage.body,
        SupportMessage.created_at,
    ]
    column_labels = {
        SupportMessage.id: "ID",
        SupportMessage.ticket_id: "Тикет",
        SupportMessage.author: "Автор",
        SupportMessage.body: "Текст",
        SupportMessage.created_at: "Создано",
    }
    column_formatters = {
        SupportMessage.author: format_message_author,
    }
    column_searchable_list = [SupportMessage.body]
    form_columns = [SupportMessage.ticket_id, SupportMessage.body]
    form_args = {
        "ticket_id": {"label": "Тикет"},
        "body": {"label": "Ответ оператора"},
    }
    can_create = True
    can_edit = False
    can_delete = False
    page_size = 50

    async def insert_model(self, request: Request, data: dict[str, Any]) -> Any:
        actor_id = session_principal_id(request)
        if actor_id is None:
            raise AppError(code="unauthorized", message="Not authenticated", status_code=401)

        ticket_raw: Any = data.get("ticket_id")
        body = str(data.get("body") or "")
        if not ticket_raw:
            ticket_raw = request.query_params.get("ticket_id")
        related_id = getattr(ticket_raw, "id", None)
        if related_id is not None:
            ticket_raw = related_id
        try:
            ticket_id = ticket_raw if isinstance(ticket_raw, UUID) else UUID(str(ticket_raw))
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="validation_error",
                message="ticket_id must be a UUID",
                status_code=400,
            ) from exc

        client_ip = request.client.host if request.client else None
        async with self.session_maker(expire_on_commit=False) as session:
            message = await operator_reply(
                session,
                ticket_id=ticket_id,
                body=body,
                actor_id=actor_id,
                ip=client_ip,
            )
        # Return to ticket chat thread after reply.
        request.state._sqladmin_after_change_response = RedirectResponse(
            str(
                request.url_for(
                    "admin:details",
                    identity="support-ticket",
                    pk=str(ticket_id),
                )
            ),
            status_code=303,
        )
        return message


class AdminPrincipalAdmin(ModelView, model=AdminPrincipal):
    category = "Доступ"
    category_icon = "fa-solid fa-shield-halved"
    name = "Оператор"
    name_plural = "Операторы"
    icon = "fa-solid fa-user-shield"
    column_list = [
        AdminPrincipal.id,
        AdminPrincipal.login,
        AdminPrincipal.is_active,
        AdminPrincipal.created_at,
        AdminPrincipal.updated_at,
    ]
    column_labels = {
        AdminPrincipal.id: "ID",
        AdminPrincipal.login: "Логин",
        AdminPrincipal.is_active: "Активен",
        AdminPrincipal.created_at: "Создан",
        AdminPrincipal.updated_at: "Обновлён",
    }
    column_formatters = {
        AdminPrincipal.is_active: lambda m, _a: "Да" if m.is_active else "Нет",
    }
    column_details_exclude_list = [AdminPrincipal.password_hash]
    form_columns = [AdminPrincipal.login, AdminPrincipal.is_active]
    form_args = {
        "login": {"label": "Логин"},
        "is_active": {"label": "Активен"},
    }
    can_create = True
    can_edit = True
    can_delete = False
    page_size = 50

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)

    async def scaffold_form(self, rules: Any = None) -> Any:
        form_cls = await super().scaffold_form(rules)

        class _Form(form_cls):  # type: ignore[misc,valid-type]
            password = PasswordField(
                "Пароль",
                validators=[Optional(), Length(min=0, max=128)],
                description="≥12 символов при создании; пусто при edit = без смены",
            )

        return _Form

    async def insert_model(self, request: Request, data: dict[str, Any]) -> Any:
        actor_id = session_principal_id(request)
        login = str(data.get("login") or "").strip()
        password = str(data.get("password") or "")
        is_active = bool(data.get("is_active", True))
        if not login or len(login) > 64:
            raise AppError(code="validation_error", message="Invalid login", status_code=400)
        if len(password) < 12:
            raise AppError(
                code="validation_error",
                message="Password must be at least 12 characters",
                status_code=400,
            )
        now = datetime.now(UTC)
        principal = AdminPrincipal(
            id=uuid4(),
            login=login,
            password_hash=hash_password(password),
            is_active=is_active,
            created_at=now,
            updated_at=now,
        )
        async with self.session_maker(expire_on_commit=False) as session:
            session.add(principal)
            await session.flush()
            session.add(
                AdminRoleBinding(
                    id=uuid4(),
                    principal_id=principal.id,
                    role="admin",
                    created_at=now,
                )
            )
            await record_audit(
                session,
                actor_id=actor_id,
                action="admin.principal_create",
                entity_type="admin_principal",
                entity_id=str(principal.id),
                ip=request.client.host if request.client else None,
            )
            await session.commit()
            await session.refresh(principal)
            return principal

    async def update_model(self, request: Request, pk: Any, data: dict[str, Any]) -> Any:
        actor_id = session_principal_id(request)
        password = str(data.get("password") or "")
        login = str(data.get("login") or "").strip()
        async with self.session_maker(expire_on_commit=False) as session:
            principal = await session.get(AdminPrincipal, UUID(str(pk)))
            if principal is None:
                raise AppError(code="not_found", message="Principal not found", status_code=404)
            if login:
                principal.login = login[:64]
            if "is_active" in data:
                principal.is_active = bool(data.get("is_active"))
            if password:
                if len(password) < 12:
                    raise AppError(
                        code="validation_error",
                        message="Password must be at least 12 characters",
                        status_code=400,
                    )
                principal.password_hash = hash_password(password)
            principal.updated_at = datetime.now(UTC)
            await record_audit(
                session,
                actor_id=actor_id,
                action="admin.principal_update",
                entity_type="admin_principal",
                entity_id=str(principal.id),
                ip=request.client.host if request.client else None,
            )
            await session.commit()
            await session.refresh(principal)
            return principal


class AdminRoleBindingAdmin(ModelView, model=AdminRoleBinding):
    category = "Доступ"
    category_icon = "fa-solid fa-shield-halved"
    name = "Роль"
    name_plural = "Роли"
    icon = "fa-solid fa-key"
    column_list = [
        AdminRoleBinding.id,
        AdminRoleBinding.principal_id,
        AdminRoleBinding.role,
        AdminRoleBinding.created_at,
    ]
    column_labels = {
        AdminRoleBinding.id: "ID",
        AdminRoleBinding.principal_id: "Оператор",
        AdminRoleBinding.role: "Роль",
        AdminRoleBinding.created_at: "Создана",
    }
    column_formatters = {
        AdminRoleBinding.role: format_admin_role,
    }
    form_columns = [
        AdminRoleBinding.principal_id,
        AdminRoleBinding.role,
    ]
    can_create = True
    can_edit = False
    can_delete = True
    page_size = 50

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)


class AdminAuditEventAdmin(ModelView, model=AdminAuditEvent):
    category = "Доступ"
    category_icon = "fa-solid fa-shield-halved"
    name = "Событие аудита"
    name_plural = "Журнал аудита"
    icon = "fa-solid fa-clipboard-list"
    column_list = [
        AdminAuditEvent.id,
        AdminAuditEvent.actor_id,
        AdminAuditEvent.action,
        AdminAuditEvent.entity_type,
        AdminAuditEvent.entity_id,
        AdminAuditEvent.ip,
        AdminAuditEvent.created_at,
    ]
    column_labels = {
        AdminAuditEvent.id: "ID",
        AdminAuditEvent.actor_id: "Актор",
        AdminAuditEvent.action: "Действие",
        AdminAuditEvent.entity_type: "Сущность",
        AdminAuditEvent.entity_id: "ID сущности",
        AdminAuditEvent.ip: "IP",
        AdminAuditEvent.created_at: "Когда",
    }
    column_searchable_list = [AdminAuditEvent.action, AdminAuditEvent.entity_type]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 100


def register_views(admin: Any, settings: Settings) -> None:
    show_debug = settings.otp_store_debug_code_enabled

    class OtpChallengeAdmin(ModelView, model=AuthOtpChallenge):
        category = "Пользователи"
        category_icon = "fa-solid fa-user-group"
        name = "OTP"
        name_plural = "OTP"
        icon = "fa-solid fa-sms"
        column_list = _otp_columns(show_debug_code=show_debug)
        column_labels = {
            AuthOtpChallenge.id: "ID",
            AuthOtpChallenge.phone_e164: "Телефон",
            AuthOtpChallenge.display_name: "Имя",
            AuthOtpChallenge.debug_code: "Код",
            AuthOtpChallenge.expires_at: "Истекает",
            AuthOtpChallenge.attempts: "Попытки",
            AuthOtpChallenge.consumed_at: "Использован",
            AuthOtpChallenge.created_at: "Создан",
        }
        column_formatters = {
            AuthOtpChallenge.debug_code: format_debug_code,
        }
        column_details_exclude_list = [AuthOtpChallenge.code_digest]
        column_searchable_list = [
            AuthOtpChallenge.phone_e164,
            AuthOtpChallenge.display_name,
        ]
        column_sortable_list = [
            AuthOtpChallenge.created_at,
            AuthOtpChallenge.expires_at,
            AuthOtpChallenge.phone_e164,
        ]
        column_default_sort = (AuthOtpChallenge.created_at, True)
        column_filters = [
            OperationColumnFilter(AuthOtpChallenge.phone_e164, title="Телефон"),
            OtpLinkedUserIdFilter(),
        ]
        can_create = False
        can_edit = False
        can_delete = False
        page_size = 50

    class PhoneChangeChallengeAdmin(ModelView, model=AuthPhoneChangeChallenge):
        category = "Пользователи"
        category_icon = "fa-solid fa-user-group"
        name = "Смена телефона"
        name_plural = "Смена телефона"
        icon = "fa-solid fa-mobile"
        column_list = _phone_change_columns(show_debug_code=show_debug)
        column_labels = {
            AuthPhoneChangeChallenge.id: "ID",
            AuthPhoneChangeChallenge.user_id: "Пользователь",
            AuthPhoneChangeChallenge.phone_e164: "Новый телефон",
            AuthPhoneChangeChallenge.debug_code: "Код",
            AuthPhoneChangeChallenge.expires_at: "Истекает",
            AuthPhoneChangeChallenge.attempts: "Попытки",
            AuthPhoneChangeChallenge.consumed_at: "Использован",
            AuthPhoneChangeChallenge.created_at: "Создан",
        }
        column_formatters = {
            AuthPhoneChangeChallenge.debug_code: format_debug_code,
            AuthPhoneChangeChallenge.user_id: format_user_id_peek,
        }
        column_details_exclude_list = [AuthPhoneChangeChallenge.code_digest]
        column_searchable_list = [AuthPhoneChangeChallenge.phone_e164]
        column_sortable_list = [
            AuthPhoneChangeChallenge.created_at,
            AuthPhoneChangeChallenge.phone_e164,
        ]
        column_default_sort = (AuthPhoneChangeChallenge.created_at, True)
        column_filters = [
            OperationColumnFilter(AuthPhoneChangeChallenge.phone_e164, title="Телефон"),
            OperationColumnFilter(AuthPhoneChangeChallenge.user_id, title="ID пользователя"),
        ]
        can_create = False
        can_edit = False
        can_delete = False
        page_size = 50

    admin.add_view(UserAdmin)
    admin.add_view(OtpChallengeAdmin)
    admin.add_view(PhoneChangeChallengeAdmin)
    admin.add_view(SupportTicketAdmin)
    admin.add_view(SupportMessageAdmin)
    admin.add_view(AdminPrincipalAdmin)
    admin.add_view(AdminRoleBindingAdmin)
    admin.add_view(AdminAuditEventAdmin)
