"""Server-side permission gates for SQLAdmin pages and custom actions."""

from __future__ import annotations

from collections.abc import Callable
from types import MethodType
from typing import Any, cast

from sqladmin import Admin, BaseView, ModelView
from sqladmin.authentication import login_required
from starlette import status
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.dashboard import build_dashboard
from tourism_backend.modules.admin.presentation.auth import require_permission
from tourism_backend.modules.admin.presentation.csrf import _origin_ok

_CATEGORY_SCOPE = {
    "Пользователи": "users",
    "Поддержка": "support",
    "Доступ": "access",
    "Маршруты": "routes",
    "Места": "places",
    "Отзывы": "reviews",
    "Достижения": "achievements",
    "Рекомендации": "recommendations",
    "География": "geography",
    "Транспорт": "transit",
    "Уведомления": "notifications",
    "Медиа": "media",
    "Контент": "content",
    "Настройки приложения": "settings",
    "Анти-фрод": "antifraud",
}


def permission_for_view(view: BaseView | ModelView, *, write: bool = False) -> str | None:
    scope = getattr(view, "permission_scope", None) or _CATEGORY_SCOPE.get(view.category)
    if scope is None:
        return None
    if write and scope == "access":
        return "access.manage"
    return f"{scope}.{'write' if write else 'read'}"


def can_write_view(request: Request, view: BaseView | ModelView) -> bool:
    permission = permission_for_view(view, write=True)
    return bool(permission and require_permission(request, permission))


class PermissionedModelView(ModelView):
    """Default gate for model screens; explicit view overrides may narrow it."""

    def is_accessible(self, request: Request) -> bool:
        permission = permission_for_view(self)
        return bool(permission and require_permission(request, permission))

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    async def check_can_create(self, request: Request) -> bool:
        permission = permission_for_view(self, write=True)
        return bool(permission and require_permission(request, permission) and self.can_create)

    async def check_can_edit(self, request: Request, model: Any) -> bool:
        permission = permission_for_view(self, write=True)
        return bool(permission and require_permission(request, permission) and self.can_edit)

    async def check_can_delete(self, request: Request, model: Any) -> bool:
        permission = permission_for_view(self, write=True)
        return bool(permission and require_permission(request, permission) and self.can_delete)

    async def check_can_import(self, request: Request) -> bool:
        permission = permission_for_view(self, write=True)
        return bool(permission and require_permission(request, permission) and self.can_import)


class PermissionedAdmin(Admin):
    """Gate SQLAdmin's exposed routes, which otherwise check login only."""

    @login_required
    async def index(self, request: Request) -> Response:
        permissions: frozenset[str] = getattr(request.state, "admin_permissions", frozenset())
        async with self.session_maker(expire_on_commit=False) as session:
            dashboard = await build_dashboard(session, permissions=permissions)
        return await self.templates.TemplateResponse(
            request, "sqladmin/index.html", context={"dashboard": dashboard}
        )

    def _protected(
        self,
        func: MethodType,
        view_instance: BaseView | ModelView,
        *,
        write: bool | None,
    ) -> Callable[[Request], Any]:
        async def protected(request: Request) -> Response:
            # SQLAdmin actions change data through GET links. Require evidence
            # that the navigation started in this admin origin as well.
            if write is True and (
                not _origin_ok(request)
                or not (
                    request.headers.get("origin")
                    or request.headers.get("referer")
                    or request.headers.get("sec-fetch-site") in {"same-origin", "same-site"}
                )
            ):
                return Response(status_code=status.HTTP_403_FORBIDDEN)
            backend = self.authentication_backend
            if backend is not None:
                result = await backend.authenticate(request)
                if isinstance(result, Response):
                    return result
                if not result:
                    return RedirectResponse(
                        request.url_for("admin:login"), status_code=status.HTTP_302_FOUND
                    )
            permission = permission_for_view(
                view_instance, write=(request.method != "GET" if write is None else write)
            )
            if (
                not view_instance.is_accessible(request)
                or permission is None
                or not require_permission(request, permission)
            ):
                return Response(status_code=status.HTTP_403_FORBIDDEN)
            # SQLAdmin's @action/@expose wrapper checks login only. The
            # original coroutine runs after our fresh auth + permission check.
            decorated: Any = func
            return cast(Response, await decorated.__wrapped__(view_instance, request))

        return protected

    def _handle_action_decorated_func(
        self,
        func: MethodType,
        view: type[BaseView | ModelView],
        view_instance: BaseView | ModelView,
    ) -> None:
        if not hasattr(func, "_action"):
            return
        decorated: Any = func
        model_view = cast(ModelView, view_instance)
        slug = decorated._slug
        self.admin.add_route(
            route=self._protected(func, view_instance, write=True),
            path=f"/{model_view.identity}/action/{slug}",
            methods=["GET"],
            name=f"action-{model_view.identity}-{slug}",
            include_in_schema=decorated._include_in_schema,
        )
        if decorated._add_in_list:
            model_view._custom_actions_in_list[slug] = decorated._label
        if decorated._add_in_detail:
            model_view._custom_actions_in_detail[slug] = decorated._label
        if decorated._confirmation_message:
            model_view._custom_actions_confirmation[slug] = decorated._confirmation_message

    def _handle_expose_decorated_func(
        self,
        func: MethodType,
        view: type[BaseView | ModelView],
        view_instance: BaseView | ModelView,
    ) -> None:
        if not hasattr(func, "_exposed"):
            return
        decorated: Any = func
        if view.is_model:
            path = f"/{view_instance.identity}" + decorated._path
            name = f"view-{view_instance.identity}-{func.__name__}"
        else:
            view.identity = decorated._identity
            path = decorated._path
            name = f"view-{view.identity}"
        methods = decorated._methods
        self.admin.add_route(
            route=self._protected(func, view_instance, write=None),
            path=path,
            methods=methods,
            name=name,
            include_in_schema=decorated._include_in_schema,
        )
