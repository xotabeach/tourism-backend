"""SQLAdmin ModelViews for ops (users, OTP, support, principals)."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqladmin import ModelView, action, expose
from sqladmin.filters import AllUniqueStringValuesFilter, OperationColumnFilter
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from wtforms import FileField, PasswordField  # type: ignore[import-untyped]
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
from tourism_backend.modules.admin.presentation.datetime_fmt import (
    ADMIN_COLUMN_TYPE_FORMATTERS,
    format_moscow_plain,
)
from tourism_backend.modules.admin.presentation.filters import (
    AwaitingOperatorReplyFilter,
    OtpLinkedUserIdFilter,
)
from tourism_backend.modules.admin.presentation.formatters import (
    format_admin_role,
    format_debug_code,
    format_message_author,
    format_review_body_preview,
    format_review_status,
    format_route_fk,
    format_route_publication_status,
    format_ticket_awaiting,
    format_ticket_kind,
    format_ticket_status,
    format_user_avatar_name,
    format_user_cover,
    format_user_fk,
    format_user_id_peek,
)
from tourism_backend.modules.identity.application import media as identity_media
from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    validate_display_name,
)
from tourism_backend.modules.identity.application.schemas import normalize_ru_phone
from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    AuthPhoneChangeChallenge,
    TravelRank,
    User,
)
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.application.service import resolve_urls
from tourism_backend.modules.routes.application import review_service
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview
from tourism_backend.modules.support.infrastructure.models import SupportMessage, SupportTicket


async def _preload_user_names(session_maker: Any, user_ids: list[UUID]) -> dict[UUID, str]:
    ids = list({uid for uid in user_ids if uid is not None})
    if not ids:
        return {}
    async with session_maker(expire_on_commit=False) as session:
        rows = (await session.scalars(select(User).where(User.id.in_(ids)))).all()
    return {row.id: row.display_name for row in rows}


async def _preload_route_names(session_maker: Any, route_ids: list[UUID]) -> dict[UUID, str]:
    ids = list({rid for rid in route_ids if rid is not None})
    if not ids:
        return {}
    async with session_maker(expire_on_commit=False) as session:
        rows = (await session.scalars(select(Route).where(Route.id.in_(ids)))).all()
    return {row.id: row.name for row in rows}


_AUTHOR_RU = {
    "user": "Пользователь",
    "operator": "Оператор",
    "assistant": "Ассистент",
    "system": "Система",
}


def _read_upload_bytes(value: Any) -> bytes | None:
    """Accept WTForms FileStorage / Starlette UploadFile / raw bytes."""
    if value is None or value == "":
        return None
    filename = getattr(value, "filename", None)
    if filename is not None and str(filename).strip() == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    read = getattr(value, "read", None)
    if not callable(read):
        return None
    raw = read()
    if hasattr(value, "seek"):
        with contextlib.suppress(OSError):
            value.seek(0)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return bytes(raw) if raw else None


class UserAdmin(ModelView, model=User):
    category = "Пользователи"
    category_icon = "fa-solid fa-user-group"
    name = "Пользователь"
    name_plural = "Пользователи"
    icon = "fa-solid fa-users"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        User.id,
        User.display_name,
        User.phone_e164,
        User.travel_points,
        User.rank_id,
        User.created_at,
        User.updated_at,
        User.notify_push_enabled,
    ]
    column_labels = {
        User.id: "Баннер",
        User.display_name: "Профиль",
        User.phone_e164: "Телефон",
        User.travel_points: "ТП",
        User.rank_id: "Звание",
        User.created_at: "Создан",
        User.updated_at: "Обновлён",
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
    column_sortable_list = [
        User.created_at,
        User.updated_at,
        User.display_name,
        User.travel_points,
        User.rank_id,
        User.phone_e164,
    ]
    column_default_sort = (User.created_at, True)
    column_filters = [
        OperationColumnFilter(User.phone_e164, title="Телефон"),
        OperationColumnFilter(User.display_name, title="Имя"),
        OperationColumnFilter(User.id, title="ID пользователя"),
    ]
    form_columns = [
        User.display_name,
        User.phone_e164,
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
        "notify_push_enabled": {"label": "Push"},
        "notify_sms_enabled": {"label": "SMS"},
        "notify_haptics_enabled": {"label": "Тактильность"},
    }
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def scaffold_form(self, rules: Any = None) -> Any:
        form_cls = await super().scaffold_form(rules)

        class _Form(form_cls):  # type: ignore[misc,valid-type]
            avatar_file = FileField(
                "Аватар",
                validators=[Optional()],
                description="JPEG / PNG / WebP, до 5 МБ. Пусто = без изменения.",
            )
            cover_file = FileField(
                "Баннер",
                validators=[Optional()],
                description="JPEG / PNG / WebP, до 5 МБ. Пусто = без изменения.",
            )

        return _Form

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

        # travel_points are auto-awarded — ignore any posted value.
        if "travel_points" in data:
            data = {k: v for k, v in data.items() if k != "travel_points"}

        avatar_bytes = _read_upload_bytes(data.get("avatar_file"))
        cover_bytes = _read_upload_bytes(data.get("cover_file"))

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
            user.notify_push_enabled = bool(data.get("notify_push_enabled"))
            user.notify_sms_enabled = bool(data.get("notify_sms_enabled"))
            user.notify_haptics_enabled = bool(data.get("notify_haptics_enabled"))
            user.updated_at = datetime.now(UTC)

            for kind, payload in (("avatar", avatar_bytes), ("cover", cover_bytes)):
                if payload is None:
                    continue
                saved = identity_media.save_profile_image_bytes(payload, user_id=user.id, kind=kind)
                await media_service.replace_attachment(
                    session,
                    entity_type="user",
                    entity_id=user.id,
                    role=kind,
                    storage_key=saved.storage_key,
                    content_type=saved.content_type,
                    byte_size=saved.byte_size,
                    width=saved.width,
                    height=saved.height,
                    uploaded_by_user_id=user.id,
                )

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


class TravelRankAdmin(ModelView, model=TravelRank):
    category = "Пользователи"
    name = "Звание"
    name_plural = "Звания"
    icon = "fa-solid fa-ranking-star"
    column_list = [
        TravelRank.sort_order,
        TravelRank.title,
        TravelRank.slug,
        TravelRank.min_points,
        TravelRank.next_rank_points,
    ]
    column_labels = {
        TravelRank.sort_order: "Порядок",
        TravelRank.title: "Название",
        TravelRank.slug: "Код",
        TravelRank.min_points: "От ТП",
        TravelRank.next_rank_points: "Следующее звание, ТП",
    }
    column_default_sort = (TravelRank.sort_order, False)
    can_create = False
    can_delete = False
    can_export = False


class SupportTicketAdmin(ModelView, model=SupportTicket):
    category = "Поддержка"
    category_icon = "fa-solid fa-headset"
    name = "Тикет"
    name_plural = "Тикеты"
    icon = "fa-solid fa-ticket"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        SupportTicket.last_human_author,
        SupportTicket.id,
        SupportTicket.user_id,
        SupportTicket.kind,
        SupportTicket.subject,
        SupportTicket.status,
        SupportTicket.last_message_at,
        SupportTicket.updated_at,
        SupportTicket.created_at,
    ]
    column_labels = {
        SupportTicket.last_human_author: "Ответ",
        SupportTicket.id: "ID",
        SupportTicket.user_id: "Пользователь",
        SupportTicket.kind: "Тип",
        SupportTicket.subject: "Тема",
        SupportTicket.status: "Статус",
        SupportTicket.last_message_at: "Последнее сообщение",
        SupportTicket.created_at: "Создан",
        SupportTicket.updated_at: "Обновлён",
    }
    column_formatters = {
        SupportTicket.status: format_ticket_status,
        SupportTicket.kind: format_ticket_kind,
        SupportTicket.user_id: format_user_id_peek,
        SupportTicket.last_human_author: format_ticket_awaiting,
    }
    column_formatters_detail = {
        SupportTicket.user_id: format_user_id_peek,
        SupportTicket.status: format_ticket_status,
        SupportTicket.kind: format_ticket_kind,
    }
    column_searchable_list = [SupportTicket.subject]
    column_sortable_list = [
        SupportTicket.last_message_at,
        SupportTicket.updated_at,
        SupportTicket.created_at,
        SupportTicket.status,
        SupportTicket.kind,
        SupportTicket.subject,
    ]
    column_default_sort = (SupportTicket.last_message_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(SupportTicket.status),
        AllUniqueStringValuesFilter(SupportTicket.kind),
        OperationColumnFilter(SupportTicket.user_id, title="ID пользователя"),
        AwaitingOperatorReplyFilter(),
    ]
    form_columns = [SupportTicket.status]
    can_create = False
    can_delete = False
    can_edit = True
    page_size = 50
    details_template = "sqladmin/support_chat.html"

    async def list(self, request: Request) -> Any:
        pagination = await super().list(request)
        request.state.user_names = await _preload_user_names(
            self.session_maker,
            [row.user_id for row in pagination.rows],
        )
        return pagination

    async def _load_chat_messages(self, ticket_id: UUID) -> Sequence[dict[str, str]]:
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
                        "created_at": format_moscow_plain(msg.created_at),
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
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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
    column_sortable_list = [SupportMessage.created_at, SupportMessage.author]
    column_default_sort = (SupportMessage.created_at, True)
    column_filters = [
        AllUniqueStringValuesFilter(SupportMessage.author),
        OperationColumnFilter(SupportMessage.ticket_id, title="ID тикета"),
    ]
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
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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


class RouteAdmin(ModelView, model=Route):
    category = "Маршруты"
    category_icon = "fa-solid fa-route"
    name = "Маршрут"
    name_plural = "Маршруты"
    icon = "fa-solid fa-map-location-dot"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        Route.publication_status,
        Route.name,
        Route.owner_user_id,
        Route.source,
        Route.visibility,
        Route.difficulty,
        Route.updated_at,
        Route.created_at,
    ]
    column_labels = {
        Route.id: "ID",
        Route.publication_status: "Статус",
        Route.name: "Название",
        Route.owner_user_id: "Автор",
        Route.source: "Источник",
        Route.visibility: "Видимость",
        Route.lifecycle_status: "Жизненный цикл",
        Route.short_description: "Краткое описание",
        Route.description: "Описание",
        Route.difficulty: "Сложность",
        Route.transport_mode: "Способ передвижения",
        Route.estimated_duration_minutes: "Длительность, мин",
        Route.distance_meters: "Расстояние, м",
        Route.suitable_for_children: "Подходит детям",
        Route.pets_allowed: "Можно с животными",
        Route.updated_at: "Обновлён",
        Route.created_at: "Создан",
    }
    column_formatters = {
        Route.publication_status: format_route_publication_status,
        Route.owner_user_id: format_user_fk,
    }
    column_formatters_detail = {
        Route.publication_status: format_route_publication_status,
        Route.owner_user_id: format_user_fk,
    }
    column_searchable_list = [Route.name, Route.description]
    column_sortable_list = [
        Route.publication_status,
        Route.updated_at,
        Route.created_at,
        Route.name,
    ]
    column_default_sort = (Route.updated_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(Route.publication_status),
        AllUniqueStringValuesFilter(Route.source),
        AllUniqueStringValuesFilter(Route.visibility),
        OperationColumnFilter(Route.owner_user_id, title="ID автора"),
    ]
    form_columns = [
        Route.name,
        Route.short_description,
        Route.description,
        Route.estimated_duration_minutes,
        Route.distance_meters,
        Route.difficulty,
        Route.transport_mode,
        Route.suitable_for_children,
        Route.pets_allowed,
    ]
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def list(self, request: Request) -> Any:
        pagination = await super().list(request)
        request.state.user_names = await _preload_user_names(
            self.session_maker,
            [row.owner_user_id for row in pagination.rows if row.owner_user_id],
        )
        return pagination

    async def get_object_for_details(self, request: Request) -> Any:
        model = await super().get_object_for_details(request)
        if model is not None and model.owner_user_id is not None:
            request.state.user_names = await _preload_user_names(
                self.session_maker,
                [model.owner_user_id],
            )
        return model

    async def _set_publication_status(
        self,
        request: Request,
        *,
        publication_status: str,
    ) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        raw_pks = request.query_params.get("pks", "")
        route_ids: list[UUID] = []
        for raw in raw_pks.split(","):
            with contextlib.suppress(ValueError):
                route_ids.append(UUID(raw.strip()))
        if route_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                routes = list(
                    (await session.scalars(select(Route).where(Route.id.in_(route_ids)))).all()
                )
                now = datetime.now(UTC)
                for route in routes:
                    if (
                        publication_status in {"published", "rejected"}
                        and route.publication_status != "pending_review"
                    ):
                        continue
                    route.publication_status = publication_status
                    route.updated_at = now
                    if publication_status == "published":
                        route.visibility = "public"
                        route.lifecycle_status = "active"
                    elif publication_status == "deleted":
                        route.visibility = "private"
                        route.lifecycle_status = "archived"
                    else:
                        route.visibility = "private"
                        route.lifecycle_status = "draft"
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=f"admin.route_{publication_status}",
                        entity_type="route",
                        entity_id=str(route.id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="approve_routes",
        label="Одобрить и опубликовать",
        confirmation_message="Опубликовать выбранные маршруты?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def approve_routes(self, request: Request) -> Response:
        return await self._set_publication_status(request, publication_status="published")

    @action(
        name="reject_routes",
        label="Вернуть на доработку",
        confirmation_message="Вернуть выбранные маршруты авторам?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reject_routes(self, request: Request) -> Response:
        return await self._set_publication_status(request, publication_status="rejected")

    @action(
        name="delete_routes",
        label="Удалить",
        confirmation_message="Скрыть и пометить выбранные маршруты удалёнными?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def delete_routes(self, request: Request) -> Response:
        return await self._set_publication_status(request, publication_status="deleted")


class RouteReviewAdmin(ModelView, model=RouteReview):
    category = "Маршруты"
    category_icon = "fa-solid fa-route"
    name = "Отзыв"
    name_plural = "Отзывы"
    icon = "fa-solid fa-comments"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteReview.status,
        RouteReview.route_id,
        RouteReview.author_user_id,
        RouteReview.rating,
        RouteReview.body,
        RouteReview.created_at,
        RouteReview.updated_at,
    ]
    column_labels = {
        RouteReview.id: "ID",
        RouteReview.status: "Статус",
        RouteReview.route_id: "Маршрут",
        RouteReview.author_user_id: "Автор",
        RouteReview.rating: "Оценка",
        RouteReview.body: "Текст",
        RouteReview.moderator_note: "Заметка модератора",
        RouteReview.moderated_at: "Модерирован",
        RouteReview.created_at: "Создан",
        RouteReview.updated_at: "Обновлён",
    }
    column_formatters = {
        RouteReview.status: format_review_status,
        RouteReview.route_id: format_route_fk,
        RouteReview.author_user_id: format_user_fk,
        RouteReview.body: format_review_body_preview,
    }
    column_formatters_detail = {
        RouteReview.status: format_review_status,
        RouteReview.route_id: format_route_fk,
        RouteReview.author_user_id: format_user_fk,
    }
    column_searchable_list = [RouteReview.body]
    column_sortable_list = [
        RouteReview.status,
        RouteReview.rating,
        RouteReview.created_at,
        RouteReview.updated_at,
    ]
    column_default_sort = (RouteReview.created_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(RouteReview.status),
        OperationColumnFilter(RouteReview.route_id, title="ID маршрута"),
        OperationColumnFilter(RouteReview.author_user_id, title="ID автора"),
        OperationColumnFilter(RouteReview.rating, title="Оценка"),
    ]
    form_columns = [
        RouteReview.moderator_note,
    ]
    form_args = {
        "moderator_note": {"label": "Заметка модератора"},
    }
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def list(self, request: Request) -> Any:
        pagination = await super().list(request)
        request.state.user_names = await _preload_user_names(
            self.session_maker,
            [row.author_user_id for row in pagination.rows],
        )
        request.state.route_names = await _preload_route_names(
            self.session_maker,
            [row.route_id for row in pagination.rows],
        )
        return pagination

    async def get_object_for_details(self, request: Request) -> Any:
        model = await super().get_object_for_details(request)
        if model is not None:
            request.state.user_names = await _preload_user_names(
                self.session_maker,
                [model.author_user_id],
            )
            request.state.route_names = await _preload_route_names(
                self.session_maker,
                [model.route_id],
            )
        return model

    async def _set_status(self, request: Request, *, status_value: str) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        raw_pks = request.query_params.get("pks", "")
        review_ids: list[UUID] = []
        for raw in raw_pks.split(","):
            with contextlib.suppress(ValueError):
                review_ids.append(UUID(raw.strip()))
        if review_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                await review_service.set_review_status(
                    session,
                    review_ids=review_ids,
                    status=status_value,
                )
                for review_id in review_ids:
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=f"admin.review_{status_value}",
                        entity_type="route_review",
                        entity_id=str(review_id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="approve_reviews",
        label="Одобрить и опубликовать",
        confirmation_message="Опубликовать выбранные отзывы?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def approve_reviews(self, request: Request) -> Response:
        return await self._set_status(request, status_value="published")

    @action(
        name="reject_reviews",
        label="Отклонить",
        confirmation_message="Отклонить выбранные отзывы?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reject_reviews(self, request: Request) -> Response:
        return await self._set_status(request, status_value="rejected")


def register_views(admin: Any, settings: Settings) -> None:
    show_debug = settings.otp_store_debug_code_enabled

    class OtpChallengeAdmin(ModelView, model=AuthOtpChallenge):
        category = "Пользователи"
        category_icon = "fa-solid fa-user-group"
        name = "OTP"
        name_plural = "OTP"
        icon = "fa-solid fa-sms"
        column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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
        column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
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
    admin.add_view(TravelRankAdmin)
    admin.add_view(OtpChallengeAdmin)
    admin.add_view(PhoneChangeChallengeAdmin)
    admin.add_view(SupportTicketAdmin)
    admin.add_view(SupportMessageAdmin)
    admin.add_view(RouteAdmin)
    admin.add_view(RouteReviewAdmin)
    admin.add_view(AdminPrincipalAdmin)
    admin.add_view(AdminRoleBindingAdmin)
    admin.add_view(AdminAuditEventAdmin)
