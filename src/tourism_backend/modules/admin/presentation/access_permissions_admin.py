"""Admin-only editor for role grants and individual permission exceptions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from sqlalchemy import delete, select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.application.permissions import PERMISSION_LABELS
from tourism_backend.modules.admin.infrastructure.models import (
    AdminPermissionOverride,
    AdminPrincipal,
    AdminRoleBinding,
    AdminRolePermission,
)
from tourism_backend.modules.admin.presentation.auth import (
    require_admin_role,
    session_principal_id,
)

EDITABLE_ROLES = ("ops", "support", "route_manager", "content_manager")
EDITABLE_PERMISSION_LABELS = {
    key: label for key, label in PERMISSION_LABELS.items() if not key.startswith("access.")
}
ROLE_LABELS = {
    "ops": "Оператор поддержки (старые учётные записи)",
    "support": "Поддержка",
    "route_manager": "Менеджер маршрутов",
    "content_manager": "Контент-менеджер",
}


class AccessPermissionsAdmin(BaseView):
    name = "Права доступа"
    category = "Доступ"
    icon = "fa-solid fa-user-lock"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_admin_role(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/access/permissions", methods=["GET", "POST"], identity="access-permissions")
    async def show(self, request: Request) -> Response:
        actor_id = session_principal_id(request)
        if actor_id is None or not require_admin_role(request):
            return Response(status_code=403)
        role = request.query_params.get("role", "support")
        if role not in EDITABLE_ROLES:
            role = "support"
        raw_principal = request.query_params.get("principal_id", "")
        try:
            selected_id = UUID(raw_principal) if raw_principal else None
        except ValueError:
            selected_id = None
        search = request.query_params.get("q", "").strip()[:64]

        async with self.session_maker(expire_on_commit=False) as session:
            if request.method == "POST":
                form = await request.form()
                kind = str(form.get("kind", ""))
                if kind == "role":
                    selected_role = str(form.get("role", ""))
                    if selected_role not in EDITABLE_ROLES:
                        return Response(status_code=400)
                    selected = {str(value) for value in form.getlist("permissions")}
                    if not selected <= EDITABLE_PERMISSION_LABELS.keys():
                        return Response(status_code=400)
                    existing_rows = await session.execute(
                        select(AdminRolePermission.permission).where(
                            AdminRolePermission.role == selected_role
                        )
                    )
                    before_grants = set(existing_rows.scalars().all())
                    await session.execute(
                        delete(AdminRolePermission).where(AdminRolePermission.role == selected_role)
                    )
                    now = datetime.now(UTC)
                    session.add_all(
                        AdminRolePermission(
                            id=uuid4(),
                            role=selected_role,
                            permission=permission,
                            created_at=now,
                        )
                        for permission in sorted(selected)
                    )
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action="admin.role_permissions_update",
                        entity_type="admin_role",
                        entity_id=selected_role,
                        metadata={"before": sorted(before_grants), "after": sorted(selected)},
                        ip=request.client.host if request.client else None,
                    )
                    await session.commit()
                    Flash.success(request, "Права роли сохранены.")
                    return RedirectResponse(
                        request.url_for("admin:view-access-permissions").include_query_params(
                            role=selected_role, principal_id=raw_principal
                        ),
                        status_code=303,
                    )
                if kind == "principal":
                    try:
                        target_id = UUID(str(form.get("principal_id", "")))
                    except ValueError:
                        return Response(status_code=400)
                    target = await session.get(AdminPrincipal, target_id)
                    if target is None:
                        return Response(status_code=404)
                    role_rows = await session.execute(
                        select(AdminRoleBinding.role).where(
                            AdminRoleBinding.principal_id == target_id
                        )
                    )
                    if "admin" in role_rows.scalars().all():
                        return Response(status_code=403)
                    effects: dict[str, str] = {}
                    for permission in EDITABLE_PERMISSION_LABELS:
                        effect = str(form.get(f"effect_{permission}", "inherit"))
                        if effect not in {"inherit", "allow", "deny"}:
                            return Response(status_code=400)
                        if effect != "inherit":
                            effects[permission] = effect
                    existing_rows = await session.execute(
                        select(
                            AdminPermissionOverride.permission, AdminPermissionOverride.effect
                        ).where(AdminPermissionOverride.principal_id == target_id)
                    )
                    before_overrides = dict(existing_rows.tuples().all())
                    await session.execute(
                        delete(AdminPermissionOverride).where(
                            AdminPermissionOverride.principal_id == target_id
                        )
                    )
                    now = datetime.now(UTC)
                    session.add_all(
                        AdminPermissionOverride(
                            id=uuid4(),
                            principal_id=target_id,
                            permission=permission,
                            effect=effect,
                            created_at=now,
                        )
                        for permission, effect in effects.items()
                    )
                    await record_audit(
                        session,
                        actor_id=actor_id,
                        action="admin.permission_overrides_update",
                        entity_type="admin_principal",
                        entity_id=str(target_id),
                        metadata={"before": before_overrides, "after": effects},
                        ip=request.client.host if request.client else None,
                    )
                    await session.commit()
                    Flash.success(request, "Персональные права сохранены.")
                    return RedirectResponse(
                        request.url_for("admin:view-access-permissions").include_query_params(
                            role=role, principal_id=str(target_id)
                        ),
                        status_code=303,
                    )
                return Response(status_code=400)

            grants_result = await session.execute(
                select(AdminRolePermission.permission).where(AdminRolePermission.role == role)
            )
            role_grants = set(grants_result.scalars().all())
            principals_query = select(AdminPrincipal)
            if search:
                principals_query = principals_query.where(AdminPrincipal.login.ilike(f"%{search}%"))
            principals_result = await session.execute(
                principals_query.order_by(AdminPrincipal.login).limit(250)
            )
            principals = principals_result.scalars().all()
            selected_principal = (
                await session.get(AdminPrincipal, selected_id) if selected_id else None
            )
            overrides: dict[str, str] = {}
            selected_is_admin = False
            if selected_principal is not None:
                overrides_result = await session.execute(
                    select(
                        AdminPermissionOverride.permission, AdminPermissionOverride.effect
                    ).where(AdminPermissionOverride.principal_id == selected_principal.id)
                )
                overrides = dict(overrides_result.tuples().all())
                role_result = await session.execute(
                    select(AdminRoleBinding.role).where(
                        AdminRoleBinding.principal_id == selected_principal.id
                    )
                )
                selected_is_admin = "admin" in role_result.scalars().all()

        return await self.templates.TemplateResponse(
            request,
            "sqladmin/access_permissions.html",
            context={
                "roles": ROLE_LABELS,
                "selected_role": role,
                "permissions": EDITABLE_PERMISSION_LABELS,
                "role_grants": role_grants,
                "principals": principals,
                "selected_principal": selected_principal,
                "selected_is_admin": selected_is_admin,
                "overrides": overrides,
                "search": search,
            },
        )
