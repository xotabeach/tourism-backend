"""SQLAdmin ModelViews for ops (users, OTP, support, principals)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqladmin import ModelView, action
from sqladmin.filters import AllUniqueStringValuesFilter
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
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
from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    AuthPhoneChangeChallenge,
    User,
)
from tourism_backend.modules.support.infrastructure.models import SupportMessage, SupportTicket


class UserAdmin(ModelView, model=User):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"
    column_list = [
        User.id,
        User.display_name,
        User.phone_e164,
        User.created_at,
        User.notify_push_enabled,
    ]
    column_searchable_list = [User.display_name, User.phone_e164]
    column_sortable_list = [User.created_at, User.display_name]
    column_details_exclude_list = []
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    page_size = 50


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
    name = "Support ticket"
    name_plural = "Support tickets"
    icon = "fa-solid fa-headset"
    column_list = [
        SupportTicket.id,
        SupportTicket.user_id,
        SupportTicket.kind,
        SupportTicket.subject,
        SupportTicket.status,
        SupportTicket.created_at,
        SupportTicket.updated_at,
    ]
    column_searchable_list = [SupportTicket.subject]
    column_filters = [AllUniqueStringValuesFilter(SupportTicket.status)]
    form_columns = [SupportTicket.status]
    can_create = False
    can_delete = False
    can_edit = True
    page_size = 50

    @action(
        name="reply_as_operator",
        label="Reply as operator",
        confirmation_message=(
            "Enter the reply body in the next prompt is not available; "
            "use Support messages → Create instead. Continue to open create form?"
        ),
        add_in_detail=True,
        add_in_list=True,
    )
    async def reply_as_operator(self, request: Request) -> Response:
        # List/detail actions only pass pks; redirect operator to message create.
        pks = request.query_params.get("pks", "")
        ticket_id = pks.split(",")[0].strip() if pks else ""
        # ModelView identity is slugify(SupportMessage) → "support-message".
        url = str(request.url_for("admin:create", identity="support-message"))
        if ticket_id:
            return RedirectResponse(f"{url}?ticket_id={ticket_id}", status_code=302)
        return RedirectResponse(url, status_code=302)


class SupportMessageAdmin(ModelView, model=SupportMessage):
    name = "Support message"
    name_plural = "Support messages"
    icon = "fa-solid fa-comments"
    column_list = [
        SupportMessage.id,
        SupportMessage.ticket_id,
        SupportMessage.author,
        SupportMessage.body,
        SupportMessage.created_at,
    ]
    column_searchable_list = [SupportMessage.body]
    form_columns = [SupportMessage.ticket_id, SupportMessage.body]
    can_create = True
    can_edit = False
    can_delete = False
    page_size = 50

    async def insert_model(self, request: Request, data: dict[str, Any]) -> Any:
        actor_id = session_principal_id(request)
        if actor_id is None:
            raise AppError(code="unauthorized", message="Not authenticated", status_code=401)

        ticket_raw = data.get("ticket_id")
        body = str(data.get("body") or "")
        # Prefer query prefill from ticket action.
        if not ticket_raw:
            ticket_raw = request.query_params.get("ticket_id")
        try:
            ticket_id = UUID(str(ticket_raw))
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="validation_error",
                message="ticket_id must be a UUID",
                status_code=400,
            ) from exc

        client_ip = request.client.host if request.client else None
        async with self.session_maker(expire_on_commit=False) as session:
            return await operator_reply(
                session,
                ticket_id=ticket_id,
                body=body,
                actor_id=actor_id,
                ip=client_ip,
            )


class AdminPrincipalAdmin(ModelView, model=AdminPrincipal):
    name = "Admin principal"
    name_plural = "Admin principals"
    icon = "fa-solid fa-user-shield"
    column_list = [
        AdminPrincipal.id,
        AdminPrincipal.login,
        AdminPrincipal.is_active,
        AdminPrincipal.created_at,
        AdminPrincipal.updated_at,
    ]
    column_details_exclude_list = [AdminPrincipal.password_hash]
    form_columns = [AdminPrincipal.is_active]
    can_create = False
    can_edit = True
    can_delete = False
    page_size = 50

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return require_admin_role(request)


class AdminRoleBindingAdmin(ModelView, model=AdminRoleBinding):
    name = "Admin role"
    name_plural = "Admin roles"
    icon = "fa-solid fa-key"
    column_list = [
        AdminRoleBinding.id,
        AdminRoleBinding.principal_id,
        AdminRoleBinding.role,
        AdminRoleBinding.created_at,
    ]
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
    name = "Audit event"
    name_plural = "Audit log"
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
    column_searchable_list = [AdminAuditEvent.action, AdminAuditEvent.entity_type]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 100


def register_views(admin: Any, settings: Settings) -> None:
    show_debug = settings.otp_store_debug_code_enabled

    class OtpChallengeAdmin(ModelView, model=AuthOtpChallenge):
        name = "OTP challenge"
        name_plural = "OTP challenges"
        icon = "fa-solid fa-sms"
        column_list = _otp_columns(show_debug_code=show_debug)
        column_details_exclude_list = [AuthOtpChallenge.code_digest]
        column_searchable_list = [
            AuthOtpChallenge.phone_e164,
            AuthOtpChallenge.display_name,
        ]
        can_create = False
        can_edit = False
        can_delete = False
        page_size = 50

    class PhoneChangeChallengeAdmin(ModelView, model=AuthPhoneChangeChallenge):
        name = "Phone-change challenge"
        name_plural = "Phone-change challenges"
        icon = "fa-solid fa-mobile"
        column_list = _phone_change_columns(show_debug_code=show_debug)
        column_details_exclude_list = [AuthPhoneChangeChallenge.code_digest]
        column_searchable_list = [AuthPhoneChangeChallenge.phone_e164]
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
