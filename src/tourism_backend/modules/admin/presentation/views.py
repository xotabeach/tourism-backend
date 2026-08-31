"""SQLAdmin ModelViews for ops (users, OTP, support, principals)."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqladmin import BaseView, ModelView, action, expose
from sqladmin.filters import AllUniqueStringValuesFilter, OperationColumnFilter
from sqladmin.flash import Flash
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from wtforms import FileField, PasswordField  # type: ignore[import-untyped]
from wtforms.validators import Length, Optional  # type: ignore[import-untyped]

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings, get_settings
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
    format_expert_status,
    format_masked_token,
    format_message_author,
    format_place_fk,
    format_place_publication_status,
    format_review_body_preview,
    format_review_media_gallery,
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
from tourism_backend.modules.geography.infrastructure.models import (
    Country,
    Locality,
    Region,
)
from tourism_backend.modules.identity.application import media as identity_media
from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    validate_display_name,
)
from tourism_backend.modules.identity.application.schemas import normalize_ru_phone
from tourism_backend.modules.identity.infrastructure.models import (
    EXPERT_RANK_ID,
    Achievement,
    AuthOtpChallenge,
    AuthPhoneChangeChallenge,
    TravelRank,
    User,
    UserAchievement,
    UserExpertStatusEvent,
)
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.application.service import resolve_urls
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.notifications.infrastructure.models import (
    DeviceToken,
    Notification,
)
from tourism_backend.modules.places.application import review_service as place_review_service
from tourism_backend.modules.places.application.publication_readiness import (
    is_ready_for_publication,
    publication_blockers,
)
from tourism_backend.modules.places.application.publication_service import facts_for_places
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceImage,
    PlaceReview,
    RoadEvent,
)
from tourism_backend.modules.recommendations.infrastructure.models import (
    RouteRecommendationDeckItem,
    RouteRecommendationFeedback,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)
from tourism_backend.modules.routes.application import review_service
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview
from tourism_backend.modules.runtime_config.application.service import (
    AI_PROVIDER_KEY,
    get_runtime_setting,
    set_runtime_setting,
)
from tourism_backend.modules.subscriptions.application import service as travel_plus_service
from tourism_backend.modules.subscriptions.infrastructure.models import TravelPlusSubscription
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


async def _preload_place_names(session_maker: Any, place_ids: list[UUID]) -> dict[UUID, str]:
    ids = list({place_id for place_id in place_ids if place_id is not None})
    if not ids:
        return {}
    async with session_maker(expire_on_commit=False) as session:
        rows = (await session.scalars(select(Place).where(Place.id.in_(ids)))).all()
    return {row.id: row.name for row in rows}


async def _preload_review_media(
    session_maker: Any,
    review_ids: list[UUID],
    *,
    entity_type: str = "review",
) -> dict[UUID, list[str]]:
    ids = list({review_id for review_id in review_ids if review_id is not None})
    if not ids:
        return {}
    async with session_maker(expire_on_commit=False) as session:
        rows = list(
            (
                await session.scalars(
                    select(MediaAttachment)
                    .where(
                        MediaAttachment.entity_type == entity_type,
                        MediaAttachment.entity_id.in_(ids),
                        MediaAttachment.role == "gallery",
                        MediaAttachment.status == "active",
                    )
                    .order_by(
                        MediaAttachment.entity_id,
                        MediaAttachment.sort_order,
                        MediaAttachment.created_at,
                    )
                )
            ).all()
        )
    result: dict[UUID, list[str]] = {}
    for row in rows:
        result.setdefault(row.entity_id, []).append(row.public_path)
    return result


_AUTHOR_RU = {
    "user": "Пользователь",
    "operator": "Оператор",
    "assistant": "Ассистент",
    "system": "Система",
}


async def _rank_id_for_expert_toggle(session: Any, *, user: User, is_expert: bool) -> UUID:
    """Rank to assign alongside an is_expert change.

    Granting expert moves the user onto the dedicated "Эксперт" rank;
    revoking it falls back to whatever their travel_points actually earn.
    """
    if is_expert:
        return EXPERT_RANK_ID
    fallback = await session.scalar(
        select(TravelRank)
        .where(TravelRank.min_points <= user.travel_points, TravelRank.id != EXPERT_RANK_ID)
        .order_by(TravelRank.min_points.desc())
        .limit(1)
    )
    return fallback.id if fallback is not None else user.rank_id


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
        User.is_expert,
        User.travel_plus_active,
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
        User.is_expert: "Эксперт",
        User.travel_plus_active: "Travel+",
        User.travel_plus_expires_at: "Travel+ до",
        User.travel_plus_plan: "Travel+ план",
        User.notify_sms_enabled: "SMS",
        User.notify_haptics_enabled: "Тактильность",
    }
    column_formatters = {
        User.id: format_user_cover,
        User.display_name: format_user_avatar_name,
        User.notify_push_enabled: lambda m, _a: "Да" if m.notify_push_enabled else "Нет",
        User.is_expert: format_expert_status,
        User.travel_plus_active: lambda m, _a: "Да" if m.travel_plus_active else "Нет",
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
        User.is_expert,
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
        "is_expert": {"label": "Эксперт"},
    }
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def _notify_expert_status(
        self,
        session: Any,
        *,
        user_id: UUID,
        is_expert: bool,
    ) -> None:
        notification = await notifications_service.create_expert_status_notification(
            session,
            user_id=user_id,
            is_expert=is_expert,
        )
        await notifications_service.maybe_push_notification(
            session,
            get_settings(),
            user_id=user_id,
            kind=notification.kind,
            title=notification.title,
            body=notification.body,
            target_type="user",
            target_id=user_id,
        )

    async def _set_expert_status(self, request: Request, *, is_expert: bool) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        user_ids: list[UUID] = []
        for raw in request.query_params.get("pks", "").split(","):
            with contextlib.suppress(ValueError):
                user_ids.append(UUID(raw.strip()))
        if user_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                users = list(
                    (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
                )
                changed_at = datetime.now(UTC)
                for user in users:
                    if user.is_expert == is_expert:
                        continue
                    user.is_expert = is_expert
                    user.rank_id = await _rank_id_for_expert_toggle(
                        session, user=user, is_expert=is_expert
                    )
                    user.updated_at = changed_at
                    session.add(
                        UserExpertStatusEvent(
                            id=uuid4(),
                            user_id=user.id,
                            is_expert=is_expert,
                            changed_by_principal_id=actor_id,
                            changed_at=changed_at,
                        )
                    )
                    await self._notify_expert_status(
                        session,
                        user_id=user.id,
                        is_expert=is_expert,
                    )
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=(
                            "admin.user_grant_expert" if is_expert else "admin.user_revoke_expert"
                        ),
                        entity_type="user",
                        entity_id=str(user.id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="grant_expert",
        label="Выдать статус эксперта",
        confirmation_message="Выдать выбранным пользователям статус эксперта?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def grant_expert(self, request: Request) -> Response:
        return await self._set_expert_status(request, is_expert=True)

    @action(
        name="revoke_expert",
        label="Снять статус эксперта",
        confirmation_message="Снять у выбранных пользователей статус эксперта?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def revoke_expert(self, request: Request) -> Response:
        return await self._set_expert_status(request, is_expert=False)

    async def _set_travel_plus(
        self,
        request: Request,
        *,
        plan: str | None,
    ) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        user_ids: list[UUID] = []
        for raw in request.query_params.get("pks", "").split(","):
            with contextlib.suppress(ValueError):
                user_ids.append(UUID(raw.strip()))
        if user_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                for user_id in user_ids:
                    if plan is None:
                        await travel_plus_service.cancel_travel_plus(
                            session,
                            user_id=user_id,
                            created_by_principal_id=actor_id,
                            commit=False,
                        )
                        action = "admin.user_revoke_travel_plus"
                    else:
                        await travel_plus_service.activate_travel_plus(
                            session,
                            user_id=user_id,
                            plan=plan,
                            source="admin",
                            created_by_principal_id=actor_id,
                            commit=False,
                        )
                        action = "admin.user_grant_travel_plus"
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=action,
                        entity_type="user",
                        entity_id=str(user_id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="grant_travel_plus_monthly",
        label="Выдать Travel+ (месяц)",
        confirmation_message="Выдать выбранным пользователям Travel+ на месяц?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def grant_travel_plus_monthly(self, request: Request) -> Response:
        return await self._set_travel_plus(request, plan="monthly")

    @action(
        name="grant_travel_plus_yearly",
        label="Выдать Travel+ (год)",
        confirmation_message="Выдать выбранным пользователям Travel+ на год?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def grant_travel_plus_yearly(self, request: Request) -> Response:
        return await self._set_travel_plus(request, plan="yearly")

    @action(
        name="revoke_travel_plus",
        label="Снять Travel+",
        confirmation_message="Снять Travel+ у выбранных пользователей?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def revoke_travel_plus(self, request: Request) -> Response:
        return await self._set_travel_plus(request, plan=None)

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
            previous_is_expert = user.is_expert
            next_is_expert = bool(data.get("is_expert"))
            user.display_name = display_name
            user.phone_e164 = phone
            user.notify_push_enabled = bool(data.get("notify_push_enabled"))
            user.notify_sms_enabled = bool(data.get("notify_sms_enabled"))
            user.notify_haptics_enabled = bool(data.get("notify_haptics_enabled"))
            user.is_expert = next_is_expert
            user.updated_at = datetime.now(UTC)
            if previous_is_expert != next_is_expert:
                user.rank_id = await _rank_id_for_expert_toggle(
                    session, user=user, is_expert=next_is_expert
                )
                session.add(
                    UserExpertStatusEvent(
                        id=uuid4(),
                        user_id=user.id,
                        is_expert=next_is_expert,
                        changed_by_principal_id=actor_id,
                        changed_at=user.updated_at,
                    )
                )
                await self._notify_expert_status(
                    session,
                    user_id=user.id,
                    is_expert=next_is_expert,
                )

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
                    settings=request.app.state.settings,
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
                settings=request.app.state.settings,
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


class UserExpertStatusEventAdmin(ModelView, model=UserExpertStatusEvent):
    category = "Пользователи"
    category_icon = "fa-solid fa-user-group"
    name = "История эксперта"
    name_plural = "История экспертов"
    icon = "fa-solid fa-certificate"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        UserExpertStatusEvent.user_id,
        UserExpertStatusEvent.is_expert,
        UserExpertStatusEvent.changed_at,
        UserExpertStatusEvent.changed_by_principal_id,
    ]
    column_labels = {
        UserExpertStatusEvent.user_id: "Пользователь",
        UserExpertStatusEvent.is_expert: "Эксперт",
        UserExpertStatusEvent.changed_at: "Изменено",
        UserExpertStatusEvent.changed_by_principal_id: "Администратор",
    }
    column_formatters = {
        UserExpertStatusEvent.user_id: format_user_id_peek,
    }
    column_default_sort = (UserExpertStatusEvent.changed_at, True)
    column_filters = [
        OperationColumnFilter(UserExpertStatusEvent.user_id, title="ID пользователя"),
        OperationColumnFilter(UserExpertStatusEvent.is_expert, title="Эксперт"),
    ]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 100


class TravelPlusSubscriptionAdmin(ModelView, model=TravelPlusSubscription):
    category = "Пользователи"
    category_icon = "fa-solid fa-user-group"
    name = "Подписка Travel+"
    name_plural = "Подписки Travel+"
    icon = "fa-solid fa-crown"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        TravelPlusSubscription.user_id,
        TravelPlusSubscription.plan,
        TravelPlusSubscription.status,
        TravelPlusSubscription.starts_at,
        TravelPlusSubscription.ends_at,
        TravelPlusSubscription.source,
        TravelPlusSubscription.created_by_principal_id,
        TravelPlusSubscription.created_at,
    ]
    column_labels = {
        TravelPlusSubscription.user_id: "Пользователь",
        TravelPlusSubscription.plan: "План",
        TravelPlusSubscription.status: "Статус",
        TravelPlusSubscription.starts_at: "Начало",
        TravelPlusSubscription.ends_at: "Окончание",
        TravelPlusSubscription.source: "Источник",
        TravelPlusSubscription.created_by_principal_id: "Администратор",
        TravelPlusSubscription.created_at: "Создано",
    }
    column_formatters = {
        TravelPlusSubscription.user_id: format_user_id_peek,
    }
    column_default_sort = (TravelPlusSubscription.created_at, True)
    column_filters = [
        OperationColumnFilter(TravelPlusSubscription.status, title="Статус"),
        OperationColumnFilter(TravelPlusSubscription.plan, title="План"),
        OperationColumnFilter(TravelPlusSubscription.source, title="Источник"),
        OperationColumnFilter(TravelPlusSubscription.user_id, title="ID пользователя"),
    ]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50


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
                    previous = route.publication_status
                    if (
                        publication_status in {"published", "rejected"}
                        and previous != "pending_review"
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
                    if (
                        previous == "pending_review"
                        and publication_status in {"published", "rejected"}
                        and route.owner_user_id is not None
                    ):
                        notif = await notifications_service.create_route_moderation_notification(
                            session,
                            owner_user_id=route.owner_user_id,
                            route_id=route.id,
                            route_name=route.name,
                            approved=publication_status == "published",
                        )
                        await notifications_service.maybe_push_notification(
                            session,
                            get_settings(),
                            user_id=route.owner_user_id,
                            kind=notif.kind,
                            title=notif.title,
                            body=notif.body,
                            target_type="route",
                            target_id=route.id,
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


class PlaceAdmin(ModelView, model=Place):
    category = "Места"
    category_icon = "fa-solid fa-location-dot"
    name = "Место"
    name_plural = "Места"
    icon = "fa-solid fa-map-pin"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        Place.publication_status,
        Place.name,
        Place.locality_id,
        Place.data_quality_status,
        Place.content_enrichment_status,
        Place.source_name,
        Place.updated_at,
    ]
    column_labels = {
        Place.id: "ID",
        Place.publication_status: "Статус",
        Place.name: "Название",
        Place.locality_id: "Город",
        Place.region_id: "Регион",
        Place.slug: "Slug",
        Place.proposed_slug: "Предложенный slug",
        Place.short_description: "Краткое описание",
        Place.description: "Описание",
        Place.address: "Адрес",
        Place.website_url: "Сайт",
        Place.contact_phone: "Телефон",
        Place.opening_hours_raw: "Часы работы (OSM)",
        Place.elevation_meters: "Высота, м",
        Place.surface: "Покрытие",
        Place.difficulty: "Сложность",
        Place.recommended_visit_minutes: "Время осмотра, мин",
        Place.is_paid: "Платное",
        Place.payment_status: "Оплата",
        Place.is_suitable_for_children: "Подходит детям",
        Place.is_suitable_for_pets: "Можно с животными",
        Place.temporary_closure_status: "Временное закрытие",
        Place.data_quality_status: "Качество данных",
        Place.content_enrichment_status: "Статус текста",
        Place.merged_into_place_id: "Объединено с",
        Place.source_name: "Источник",
        Place.source_url: "Ссылка на источник",
        Place.updated_at: "Обновлено",
        Place.created_at: "Создано",
    }
    column_formatters = {
        Place.publication_status: format_place_publication_status,
    }
    column_formatters_detail = {
        Place.publication_status: format_place_publication_status,
    }
    column_searchable_list = [Place.name, Place.description, Place.address]
    column_sortable_list = [
        Place.publication_status,
        Place.name,
        Place.updated_at,
        Place.created_at,
    ]
    column_default_sort = (Place.updated_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(Place.publication_status),
        AllUniqueStringValuesFilter(Place.data_quality_status),
        AllUniqueStringValuesFilter(Place.content_enrichment_status),
        AllUniqueStringValuesFilter(Place.source_name),
        OperationColumnFilter(Place.locality_id, title="ID города"),
    ]
    form_columns = [
        Place.name,
        Place.short_description,
        Place.description,
        Place.address,
        Place.website_url,
        Place.contact_phone,
        Place.opening_hours_raw,
        Place.difficulty,
        Place.recommended_visit_minutes,
        Place.is_suitable_for_children,
        Place.is_suitable_for_pets,
        Place.temporary_closure_status,
    ]
    # Places come from the import pipeline, never hand-created here; deletion
    # would orphan route stops, so archiving is the only removal path.
    can_create = False
    can_edit = True
    can_delete = False
    can_export = False
    page_size = 50

    async def _set_publication_status(
        self,
        request: Request,
        *,
        publication_status: str,
        enforce_gate: bool,
    ) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        raw_pks = request.query_params.get("pks", "")
        place_ids: list[UUID] = []
        for raw in raw_pks.split(","):
            with contextlib.suppress(ValueError):
                place_ids.append(UUID(raw.strip()))
        if place_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                facts = await facts_for_places(session, place_ids) if enforce_gate else {}
                places = list(
                    (await session.scalars(select(Place).where(Place.id.in_(place_ids)))).all()
                )
                now = datetime.now(UTC)
                for place in places:
                    if enforce_gate:
                        place_facts = facts.get(place.id)
                        # Fail closed: a place we could not evaluate is not
                        # publishable, and neither is one with open blockers.
                        if place_facts is None or not is_ready_for_publication(place_facts):
                            blockers = (
                                ", ".join(publication_blockers(place_facts))
                                if place_facts
                                else "не удалось проверить готовность"
                            )
                            await record_audit(
                                session,
                                actor_id=actor_id,
                                action="admin.place_publish_blocked",
                                entity_type="place",
                                entity_id=str(place.id),
                                ip=request.client.host if request.client else None,
                                metadata={"blockers": blockers},
                            )
                            continue
                    place.publication_status = publication_status
                    place.updated_at = now
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=f"admin.place_{publication_status}",
                        entity_type="place",
                        entity_id=str(place.id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="publish_places",
        label="Опубликовать",
        confirmation_message=(
            "Опубликовать выбранные места? Места, не прошедшие проверку готовности, "
            "будут пропущены."
        ),
        add_in_detail=True,
        add_in_list=True,
    )
    async def publish_places(self, request: Request) -> Response:
        return await self._set_publication_status(
            request,
            publication_status="published",
            enforce_gate=True,
        )

    @action(
        name="reject_places",
        label="Отклонить",
        confirmation_message="Отклонить выбранные места?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reject_places(self, request: Request) -> Response:
        return await self._set_publication_status(
            request,
            publication_status="rejected",
            enforce_gate=False,
        )

    @action(
        name="unpublish_places",
        label="Вернуть в черновики",
        confirmation_message="Снять выбранные места с публикации?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def unpublish_places(self, request: Request) -> Response:
        return await self._set_publication_status(
            request,
            publication_status="draft",
            enforce_gate=False,
        )


class RouteReviewAdmin(ModelView, model=RouteReview):
    category = "Отзывы"
    category_icon = "fa-solid fa-comments"
    name = "Отзыв"
    name_plural = "Отзывы маршрутов"
    icon = "fa-solid fa-comments"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteReview.id,
        RouteReview.status,
        RouteReview.route_id,
        RouteReview.author_user_id,
        RouteReview.rating,
        RouteReview.body,
        RouteReview.created_at,
        RouteReview.updated_at,
    ]
    column_labels = {
        RouteReview.id: "Фото",
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
        RouteReview.id: format_review_media_gallery,
        RouteReview.status: format_review_status,
        RouteReview.route_id: format_route_fk,
        RouteReview.author_user_id: format_user_fk,
        RouteReview.body: format_review_body_preview,
    }
    column_formatters_detail = {
        RouteReview.id: format_review_media_gallery,
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
        request.state.review_media = await _preload_review_media(
            self.session_maker,
            [row.id for row in pagination.rows],
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
            request.state.review_media = await _preload_review_media(
                self.session_maker,
                [model.id],
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


class PlaceReviewAdmin(ModelView, model=PlaceReview):
    category = "Отзывы"
    category_icon = "fa-solid fa-comments"
    name = "Отзыв локации"
    name_plural = "Отзывы локаций"
    icon = "fa-solid fa-location-dot"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        PlaceReview.id,
        PlaceReview.status,
        PlaceReview.place_id,
        PlaceReview.author_user_id,
        PlaceReview.rating,
        PlaceReview.body,
        PlaceReview.created_at,
    ]
    column_labels = {
        PlaceReview.id: "Фото",
        PlaceReview.status: "Статус",
        PlaceReview.place_id: "Локация",
        PlaceReview.author_user_id: "Автор",
        PlaceReview.rating: "Оценка",
        PlaceReview.body: "Текст",
        PlaceReview.moderator_note: "Заметка модератора",
        PlaceReview.moderated_at: "Модерирован",
        PlaceReview.created_at: "Создан",
    }
    column_formatters = {
        PlaceReview.id: format_review_media_gallery,
        PlaceReview.status: format_review_status,
        PlaceReview.place_id: format_place_fk,
        PlaceReview.author_user_id: format_user_fk,
        PlaceReview.body: format_review_body_preview,
    }
    column_formatters_detail = column_formatters
    column_searchable_list = [PlaceReview.body]
    column_sortable_list = [
        PlaceReview.status,
        PlaceReview.rating,
        PlaceReview.created_at,
    ]
    column_default_sort = (PlaceReview.created_at, True)
    column_filters: ClassVar[list[Any]] = [
        AllUniqueStringValuesFilter(PlaceReview.status),
        OperationColumnFilter(PlaceReview.place_id, title="ID локации"),
        OperationColumnFilter(PlaceReview.author_user_id, title="ID автора"),
        OperationColumnFilter(PlaceReview.rating, title="Оценка"),
    ]
    form_columns = [PlaceReview.moderator_note]
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
        request.state.place_names = await _preload_place_names(
            self.session_maker,
            [row.place_id for row in pagination.rows],
        )
        request.state.review_media = await _preload_review_media(
            self.session_maker,
            [row.id for row in pagination.rows],
            entity_type="place_review",
        )
        return pagination

    async def get_object_for_details(self, request: Request) -> Any:
        model = await super().get_object_for_details(request)
        if model is not None:
            request.state.user_names = await _preload_user_names(
                self.session_maker,
                [model.author_user_id],
            )
            request.state.place_names = await _preload_place_names(
                self.session_maker,
                [model.place_id],
            )
            request.state.review_media = await _preload_review_media(
                self.session_maker,
                [model.id],
                entity_type="place_review",
            )
        return model

    async def _set_status(self, request: Request, *, status_value: str) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None:
            return RedirectResponse(str(request.url_for("admin:login")), status_code=302)
        review_ids: list[UUID] = []
        for raw in request.query_params.get("pks", "").split(","):
            with contextlib.suppress(ValueError):
                review_ids.append(UUID(raw.strip()))
        if review_ids:
            async with self.session_maker(expire_on_commit=False) as session:
                await place_review_service.set_review_status(
                    session,
                    review_ids=review_ids,
                    status=status_value,
                )
                for review_id in review_ids:
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action=f"admin.place_review_{status_value}",
                        entity_type="place_review",
                        entity_id=str(review_id),
                        ip=request.client.host if request.client else None,
                    )
                await session.commit()
        return RedirectResponse(
            str(request.url_for("admin:list", identity=self.identity)),
            status_code=303,
        )

    @action(
        name="approve_place_reviews",
        label="Одобрить и опубликовать",
        confirmation_message="Опубликовать выбранные отзывы локаций?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def approve_reviews(self, request: Request) -> Response:
        return await self._set_status(request, status_value="published")

    @action(
        name="reject_place_reviews",
        label="Отклонить",
        confirmation_message="Отклонить выбранные отзывы локаций?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def reject_reviews(self, request: Request) -> Response:
        return await self._set_status(request, status_value="rejected")


# ---------------------------------------------------------------------------
# Achievements, executions, recommendations, geography, notifications & media.
#
# These tables previously had no interface at all, so answering "why is this
# user's deck empty" or "what did this traveller actually finish" meant opening
# psql. Anything the app derives (executions, deck rows, feedback, device
# tokens) is read-only here: editing it by hand would desynchronise awarded
# points, ranks and idempotency guards.
# ---------------------------------------------------------------------------


class AchievementAdmin(ModelView, model=Achievement):
    category = "Достижения"
    category_icon = "fa-solid fa-trophy"
    name = "Достижение"
    name_plural = "Достижения"
    icon = "fa-solid fa-medal"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        Achievement.sort_order,
        Achievement.title,
        Achievement.slug,
        Achievement.icon_slug,
        Achievement.description,
        Achievement.how_to_earn,
    ]
    column_labels = {
        Achievement.sort_order: "Порядок",
        Achievement.title: "Название",
        Achievement.slug: "Slug",
        Achievement.icon_slug: "Иконка",
        Achievement.description: "Описание",
        Achievement.how_to_earn: "Как получить",
    }
    form_columns = [
        Achievement.title,
        Achievement.description,
        Achievement.how_to_earn,
        Achievement.icon_slug,
        Achievement.sort_order,
    ]
    column_searchable_list = [Achievement.title, Achievement.slug]
    column_sortable_list = [Achievement.sort_order, Achievement.title]
    column_default_sort = (Achievement.sort_order, False)
    can_create = False
    can_delete = False
    can_export = False
    page_size = 50


class UserAchievementAdmin(ModelView, model=UserAchievement):
    category = "Достижения"
    category_icon = "fa-solid fa-trophy"
    name = "Выданное достижение"
    name_plural = "Выданные достижения"
    icon = "fa-solid fa-award"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        UserAchievement.user_id,
        UserAchievement.achievement_id,
        UserAchievement.unlocked_at,
    ]
    column_labels = {
        UserAchievement.user_id: "Пользователь",
        UserAchievement.achievement_id: "Достижение",
        UserAchievement.unlocked_at: "Получено",
    }
    column_formatters = {UserAchievement.user_id: format_user_fk}
    column_sortable_list = [UserAchievement.unlocked_at]
    column_default_sort = (UserAchievement.unlocked_at, True)
    column_filters = [OperationColumnFilter(UserAchievement.user_id, title="ID пользователя")]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class RouteExecutionAdmin(ModelView, model=RouteExecution):
    category = "Достижения"
    category_icon = "fa-solid fa-trophy"
    name = "Прохождение"
    name_plural = "Прохождения"
    icon = "fa-solid fa-person-hiking"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteExecution.user_id,
        RouteExecution.route_name,
        RouteExecution.status,
        RouteExecution.awarded_points,
        RouteExecution.started_at,
        RouteExecution.completed_at,
    ]
    column_labels = {
        RouteExecution.user_id: "Пользователь",
        RouteExecution.route_id: "Маршрут",
        RouteExecution.route_name: "Название маршрута",
        RouteExecution.status: "Статус",
        RouteExecution.awarded_points: "Начислено тп",
        RouteExecution.started_at: "Начато",
        RouteExecution.completed_at: "Завершено",
        RouteExecution.cancelled_at: "Отменено",
    }
    column_formatters = {
        RouteExecution.user_id: format_user_fk,
        RouteExecution.route_id: format_route_fk,
    }
    column_sortable_list = [RouteExecution.started_at, RouteExecution.status]
    column_default_sort = (RouteExecution.started_at, True)
    column_filters = [
        OperationColumnFilter(RouteExecution.user_id, title="ID пользователя"),
        OperationColumnFilter(RouteExecution.status, title="Статус"),
    ]
    # Editing a finished run by hand would desync awarded points and ranks.
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class RouteExecutionStopAdmin(ModelView, model=RouteExecutionStop):
    category = "Достижения"
    category_icon = "fa-solid fa-trophy"
    name = "Остановка прохождения"
    name_plural = "Остановки прохождений"
    icon = "fa-solid fa-flag-checkered"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteExecutionStop.execution_id,
        RouteExecutionStop.position,
        RouteExecutionStop.place_name,
        RouteExecutionStop.is_optional,
        RouteExecutionStop.completed_at,
    ]
    column_labels = {
        RouteExecutionStop.execution_id: "Прохождение",
        RouteExecutionStop.position: "№",
        RouteExecutionStop.place_name: "Место",
        RouteExecutionStop.is_optional: "Необязательная",
        RouteExecutionStop.completed_at: "Отмечена",
    }
    column_sortable_list = [RouteExecutionStop.position]
    column_filters = [
        OperationColumnFilter(RouteExecutionStop.execution_id, title="ID прохождения")
    ]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class RecommendationDeckItemAdmin(ModelView, model=RouteRecommendationDeckItem):
    category = "Рекомендации"
    category_icon = "fa-solid fa-wand-magic-sparkles"
    name = "Карточка колоды"
    name_plural = "Колоды рекомендаций"
    icon = "fa-solid fa-layer-group"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteRecommendationDeckItem.deck_date,
        RouteRecommendationDeckItem.user_id,
        RouteRecommendationDeckItem.rank,
        RouteRecommendationDeckItem.route_id,
        RouteRecommendationDeckItem.score,
        RouteRecommendationDeckItem.explanation_code,
        RouteRecommendationDeckItem.exploration,
    ]
    column_labels = {
        RouteRecommendationDeckItem.deck_date: "Дата колоды",
        RouteRecommendationDeckItem.user_id: "Пользователь",
        RouteRecommendationDeckItem.rank: "Позиция",
        RouteRecommendationDeckItem.route_id: "Маршрут",
        RouteRecommendationDeckItem.score: "Оценка",
        RouteRecommendationDeckItem.explanation_code: "Причина",
        RouteRecommendationDeckItem.exploration: "Исследование",
        RouteRecommendationDeckItem.ranker_version: "Версия ранкера",
    }
    column_formatters = {
        RouteRecommendationDeckItem.user_id: format_user_fk,
        RouteRecommendationDeckItem.route_id: format_route_fk,
    }
    column_sortable_list = [
        RouteRecommendationDeckItem.deck_date,
        RouteRecommendationDeckItem.rank,
    ]
    column_default_sort = (RouteRecommendationDeckItem.deck_date, True)
    column_filters = [
        OperationColumnFilter(RouteRecommendationDeckItem.user_id, title="ID пользователя")
    ]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class RecommendationFeedbackAdmin(ModelView, model=RouteRecommendationFeedback):
    category = "Рекомендации"
    category_icon = "fa-solid fa-wand-magic-sparkles"
    name = "Реакция"
    name_plural = "Реакции на рекомендации"
    icon = "fa-solid fa-thumbs-down"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RouteRecommendationFeedback.created_at,
        RouteRecommendationFeedback.user_id,
        RouteRecommendationFeedback.route_id,
        RouteRecommendationFeedback.action,
        RouteRecommendationFeedback.deck_date,
    ]
    column_labels = {
        RouteRecommendationFeedback.created_at: "Когда",
        RouteRecommendationFeedback.user_id: "Пользователь",
        RouteRecommendationFeedback.route_id: "Маршрут",
        RouteRecommendationFeedback.action: "Действие",
        RouteRecommendationFeedback.deck_date: "Дата колоды",
    }
    column_formatters = {
        RouteRecommendationFeedback.user_id: format_user_fk,
        RouteRecommendationFeedback.route_id: format_route_fk,
    }
    column_sortable_list = [RouteRecommendationFeedback.created_at]
    column_default_sort = (RouteRecommendationFeedback.created_at, True)
    column_filters = [
        OperationColumnFilter(RouteRecommendationFeedback.user_id, title="ID пользователя")
    ]
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


class CountryAdmin(ModelView, model=Country):
    category = "География"
    category_icon = "fa-solid fa-earth-europe"
    name = "Страна"
    name_plural = "Страны"
    icon = "fa-solid fa-flag"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [Country.name, Country.code, Country.slug, Country.status]
    column_labels = {
        Country.name: "Название",
        Country.code: "Код",
        Country.slug: "Slug",
        Country.status: "Статус",
        Country.timezone: "Часовой пояс",
    }
    form_columns = [Country.name, Country.status, Country.timezone]
    column_searchable_list = [Country.name]
    can_create = False
    can_delete = False
    can_export = False
    page_size = 50


class RegionAdmin(ModelView, model=Region):
    category = "География"
    category_icon = "fa-solid fa-earth-europe"
    name = "Регион"
    name_plural = "Регионы"
    icon = "fa-solid fa-map"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [Region.name, Region.slug, Region.status, Region.timezone]
    column_labels = {
        Region.name: "Название",
        Region.slug: "Slug",
        Region.status: "Статус",
        Region.timezone: "Часовой пояс",
    }
    form_columns = [Region.name, Region.status, Region.timezone]
    column_searchable_list = [Region.name]
    can_create = False
    can_delete = False
    can_export = False
    page_size = 50


class LocalityAdmin(ModelView, model=Locality):
    category = "География"
    category_icon = "fa-solid fa-earth-europe"
    name = "Населённый пункт"
    name_plural = "Населённые пункты"
    icon = "fa-solid fa-city"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [Locality.name, Locality.slug, Locality.type, Locality.status]
    column_labels = {
        Locality.name: "Название",
        Locality.slug: "Slug",
        Locality.type: "Тип",
        Locality.status: "Статус",
    }
    form_columns = [Locality.name, Locality.type, Locality.status]
    column_searchable_list = [Locality.name]
    column_sortable_list = [Locality.name]
    can_create = False
    can_delete = False
    can_export = False
    page_size = 50


class CategoryAdmin(ModelView, model=Category):
    category = "География"
    category_icon = "fa-solid fa-earth-europe"
    name = "Категория"
    name_plural = "Категории мест"
    icon = "fa-solid fa-tags"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        Category.sort_order,
        Category.name,
        Category.code,
        Category.slug,
        Category.status,
    ]
    column_labels = {
        Category.sort_order: "Порядок",
        Category.name: "Название",
        Category.code: "Код",
        Category.slug: "Slug",
        Category.status: "Статус",
        Category.description: "Описание",
        Category.icon_key: "Иконка",
    }
    form_columns = [
        Category.name,
        Category.description,
        Category.icon_key,
        Category.sort_order,
        Category.status,
    ]
    column_searchable_list = [Category.name]
    column_default_sort = (Category.sort_order, False)
    can_create = False
    can_delete = False
    can_export = False
    page_size = 50


class RoadEventAdmin(ModelView, model=RoadEvent):
    category = "География"
    category_icon = "fa-solid fa-earth-europe"
    name = "Дорожное событие"
    name_plural = "Дорожные события"
    icon = "fa-solid fa-triangle-exclamation"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        RoadEvent.title,
        RoadEvent.status,
        RoadEvent.event_kind,
        RoadEvent.starts_at,
        RoadEvent.ends_at,
    ]
    column_labels = {
        RoadEvent.title: "Заголовок",
        RoadEvent.description: "Описание",
        RoadEvent.status: "Статус",
        RoadEvent.event_kind: "Тип",
        RoadEvent.starts_at: "Начало",
        RoadEvent.ends_at: "Окончание",
        RoadEvent.affects_transport: "Влияет на транспорт",
        RoadEvent.source_url: "Источник",
    }
    form_columns = [
        RoadEvent.title,
        RoadEvent.description,
        RoadEvent.status,
        RoadEvent.event_kind,
        RoadEvent.starts_at,
        RoadEvent.ends_at,
        RoadEvent.affects_transport,
    ]
    column_searchable_list = [RoadEvent.title]
    column_sortable_list = [RoadEvent.starts_at, RoadEvent.status]
    column_default_sort = (RoadEvent.starts_at, True)
    can_export = False
    page_size = 50


class NotificationAdmin(ModelView, model=Notification):
    category = "Уведомления"
    category_icon = "fa-solid fa-bell"
    name = "Уведомление"
    name_plural = "Уведомления"
    icon = "fa-solid fa-envelope"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        Notification.created_at,
        Notification.user_id,
        Notification.kind,
        Notification.title,
        Notification.is_read,
    ]
    column_labels = {
        Notification.created_at: "Когда",
        Notification.user_id: "Пользователь",
        Notification.kind: "Тип",
        Notification.title: "Заголовок",
        Notification.body: "Текст",
        Notification.is_read: "Прочитано",
    }
    column_formatters = {Notification.user_id: format_user_fk}
    column_sortable_list = [Notification.created_at]
    column_default_sort = (Notification.created_at, True)
    column_filters = [OperationColumnFilter(Notification.user_id, title="ID пользователя")]
    can_create = False
    can_edit = False
    can_export = False
    page_size = 50


class DeviceTokenAdmin(ModelView, model=DeviceToken):
    category = "Уведомления"
    category_icon = "fa-solid fa-bell"
    name = "Токен устройства"
    name_plural = "Токены устройств"
    icon = "fa-solid fa-mobile-screen"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        DeviceToken.user_id,
        DeviceToken.platform,
        DeviceToken.token,
        DeviceToken.updated_at,
    ]
    column_labels = {
        DeviceToken.user_id: "Пользователь",
        DeviceToken.platform: "Платформа",
        DeviceToken.token: "Токен",
        DeviceToken.updated_at: "Обновлён",
    }
    # A push token is a credential: show only enough to match a device.
    column_formatters = {
        DeviceToken.user_id: format_user_fk,
        DeviceToken.token: format_masked_token,
    }
    column_details_exclude_list = [DeviceToken.token]
    column_sortable_list = [DeviceToken.updated_at]
    column_default_sort = (DeviceToken.updated_at, True)
    column_filters = [OperationColumnFilter(DeviceToken.user_id, title="ID пользователя")]
    can_create = False
    can_edit = False
    can_export = False
    page_size = 50


class PlaceImageAdmin(ModelView, model=PlaceImage):
    category = "Медиа"
    category_icon = "fa-solid fa-photo-film"
    name = "Фото места"
    name_plural = "Фото мест"
    icon = "fa-solid fa-image"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        PlaceImage.place_id,
        PlaceImage.sort_order,
        PlaceImage.is_cover,
        PlaceImage.status,
        PlaceImage.author,
        PlaceImage.license,
    ]
    column_labels = {
        PlaceImage.place_id: "Место",
        PlaceImage.sort_order: "Порядок",
        PlaceImage.is_cover: "Обложка",
        PlaceImage.status: "Статус",
        PlaceImage.author: "Автор",
        PlaceImage.license: "Лицензия",
        PlaceImage.alt_text: "Alt-текст",
    }
    column_formatters = {PlaceImage.place_id: format_place_fk}
    form_columns = [
        PlaceImage.alt_text,
        PlaceImage.sort_order,
        PlaceImage.is_cover,
        PlaceImage.status,
    ]
    column_filters = [OperationColumnFilter(PlaceImage.place_id, title="ID места")]
    can_create = False
    can_export = False
    page_size = 50


# (key, human label) — deliberately excludes AIProvider.OLLAMA: it's a valid
# config enum value but ai_factory.get_ai_planning_provider() has no adapter
# for it yet, so offering it here would let an admin switch the chat to a
# provider that immediately fails every turn.
_SELECTABLE_AI_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("mock", "Mock — заглушка без реального ИИ (dev/test)"),
    ("lmstudio", "LM Studio — локальная модель (домашний хаб)"),
    ("gemini", "Gemini API — облако (Google)"),
)


class RuntimeConfigAdmin(BaseView):
    """Runtime-configurable switches that override static env config on read.

    Workstream B/E: the AI provider toggle needed a place to live that
    doesn't require a redeploy — see
    ``tourism_backend.modules.runtime_config`` for the read/write service
    and ``docs/ai-dual-provider-content-backlog-2026-08-31.md`` for why.
    Gated to the ``admin`` role, not ``ops``: a wrong provider here breaks
    the AI chat for every user until corrected.
    """

    name = "AI-провайдер"
    category = "Конфигурация"
    category_icon = "fa-solid fa-sliders"
    icon = "fa-solid fa-robot"

    # add_base_view() (unlike add_model_view()) never wires this — BaseView
    # has no bound model for SQLAdmin to infer a session from — so
    # register_views() sets it manually after admin.add_view(...). Typed
    # Any: sqladmin's SESSION_MAKER alias lives in a private (_types) module.
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)

    @expose("/config/ai-provider", methods=["GET"], identity="config-ai-provider")
    async def show(self, request: Request) -> Response:
        if not require_admin_role(request):
            Flash.error(request, "Доступно только роли admin.")
            return RedirectResponse(request.url_for("admin:index"), status_code=303)
        settings: Settings = request.app.state.settings
        async with self.session_maker(expire_on_commit=False) as session:
            override = await get_runtime_setting(session, AI_PROVIDER_KEY)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/runtime_config.html",
            context=self._context(settings, override),
        )

    @expose("/config/ai-provider/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        redirect_url = request.url_for("admin:view-config-ai-provider")
        if not require_admin_role(request):
            Flash.error(request, "Доступно только роли admin.")
            return RedirectResponse(redirect_url, status_code=303)

        form = await request.form()
        value = str(form.get("ai_provider") or "").strip()
        allowed = {key for key, _label in _SELECTABLE_AI_PROVIDERS}
        if value not in allowed:
            Flash.error(request, "Недопустимое значение провайдера ИИ.")
            return RedirectResponse(redirect_url, status_code=303)

        actor_id = session_principal_id(request)
        async with self.session_maker(expire_on_commit=False) as session:
            await set_runtime_setting(
                session,
                key=AI_PROVIDER_KEY,
                value=value,
                updated_by_principal_id=actor_id,
            )
            await record_audit(
                session,
                actor_id=actor_id,
                action="runtime_config.ai_provider.update",
                entity_type="runtime_setting",
                entity_id=AI_PROVIDER_KEY,
                metadata={"value": value},
                ip=request.client.host if request.client else None,
                commit=True,
            )
        Flash.success(
            request,
            f"Провайдер ИИ переключён на «{value}». Подхватится на следующем ходу чата, "
            "без перезапуска бэкенда.",
        )
        return RedirectResponse(redirect_url, status_code=303)

    def _context(self, settings: Settings, override: str | None) -> dict[str, Any]:
        return {
            "env_default": settings.ai_provider.value,
            "override": override,
            "effective": override or settings.ai_provider.value,
            "providers": _SELECTABLE_AI_PROVIDERS,
            "gemini_key_configured": bool(
                settings.gemini_api_key and settings.gemini_api_key.get_secret_value().strip()
            ),
            "lmstudio_configured": bool(settings.lm_studio_base_url and settings.lm_studio_model),
        }


class MediaAttachmentAdmin(ModelView, model=MediaAttachment):
    category = "Медиа"
    category_icon = "fa-solid fa-photo-film"
    name = "Вложение"
    name_plural = "Вложения"
    icon = "fa-solid fa-paperclip"
    column_type_formatters = ADMIN_COLUMN_TYPE_FORMATTERS
    column_list = [
        MediaAttachment.entity_type,
        MediaAttachment.entity_id,
        MediaAttachment.role,
        MediaAttachment.status,
        MediaAttachment.byte_size,
        MediaAttachment.sort_order,
    ]
    column_labels = {
        MediaAttachment.entity_type: "Тип сущности",
        MediaAttachment.entity_id: "ID сущности",
        MediaAttachment.role: "Роль",
        MediaAttachment.status: "Статус",
        MediaAttachment.byte_size: "Размер",
        MediaAttachment.sort_order: "Порядок",
        MediaAttachment.public_path: "Путь",
    }
    column_sortable_list = [MediaAttachment.byte_size]
    column_filters = [OperationColumnFilter(MediaAttachment.entity_id, title="ID сущности")]
    form_columns = [MediaAttachment.status, MediaAttachment.sort_order, MediaAttachment.alt_text]
    can_create = False
    can_export = False
    page_size = 50


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
    admin.add_view(UserExpertStatusEventAdmin)
    admin.add_view(TravelPlusSubscriptionAdmin)
    admin.add_view(TravelRankAdmin)
    admin.add_view(OtpChallengeAdmin)
    admin.add_view(PhoneChangeChallengeAdmin)
    admin.add_view(SupportTicketAdmin)
    admin.add_view(SupportMessageAdmin)
    admin.add_view(PlaceAdmin)
    admin.add_view(RouteAdmin)
    admin.add_view(RouteReviewAdmin)
    admin.add_view(PlaceReviewAdmin)
    admin.add_view(AdminPrincipalAdmin)
    admin.add_view(AdminRoleBindingAdmin)
    admin.add_view(AdminAuditEventAdmin)
    admin.add_view(AchievementAdmin)
    admin.add_view(UserAchievementAdmin)
    admin.add_view(RouteExecutionAdmin)
    admin.add_view(RouteExecutionStopAdmin)
    admin.add_view(RecommendationDeckItemAdmin)
    admin.add_view(RecommendationFeedbackAdmin)
    admin.add_view(CountryAdmin)
    admin.add_view(RegionAdmin)
    admin.add_view(LocalityAdmin)
    admin.add_view(CategoryAdmin)
    admin.add_view(RoadEventAdmin)
    admin.add_view(NotificationAdmin)
    admin.add_view(DeviceTokenAdmin)
    admin.add_view(PlaceImageAdmin)
    admin.add_view(MediaAttachmentAdmin)
    admin.add_view(RuntimeConfigAdmin)
    # add_base_view (unlike add_model_view) does not wire session_maker —
    # BaseView has no bound model for SQLAdmin to infer a session from. A
    # real sqladmin.Admin always has one; lightweight `register_views(fake,
    # settings)` test doubles used to introspect ModelView attributes may
    # not, so this stays optional rather than a hard requirement.
    session_maker = getattr(admin, "session_maker", None)
    if session_maker is not None:
        RuntimeConfigAdmin.session_maker = session_maker
