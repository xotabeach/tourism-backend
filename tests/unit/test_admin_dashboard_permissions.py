"""Role isolation on the new admin landing page."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from tourism_backend.config import Settings
from tourism_backend.modules.admin.application.dashboard import build_dashboard
from tourism_backend.modules.admin.application.permissions import (
    PERMISSION_LABELS,
    effective_permissions,
)
from tourism_backend.modules.admin.presentation.auth import AdminAuthBackend, require_permission
from tourism_backend.modules.admin.presentation.permissions import PermissionedAdmin
from tourism_backend.modules.admin.presentation.views import AdminRoleBindingAdmin, RouteAdmin
from tourism_backend.modules.app_stats.application.queries import StatsReport, VersionShareDay


def test_individual_deny_overrides_role_and_individual_allow() -> None:
    permissions = effective_permissions(
        ["support"],
        ["support.read", "support.write"],
        [
            ("routes.read", "allow"),
            ("support.write", "deny"),
            ("support.write", "allow"),
            ("unknown", "allow"),
        ],
    )
    assert permissions == frozenset({"support.read", "routes.read"})
    assert effective_permissions(["admin"], [], []) == frozenset(PERMISSION_LABELS)


def test_route_screen_requires_fresh_route_permission() -> None:
    request = Request({"type": "http", "session": {}})  # type: ignore[arg-type]
    route_view = RouteAdmin()
    request.state.admin_permissions = frozenset({"content.read"})
    assert route_view.is_accessible(request) is False
    request.state.admin_permissions = frozenset({"routes.read"})
    assert route_view.is_accessible(request) is True


def test_stats_cards_use_selected_day_not_incomplete_today() -> None:
    yesterday = VersionShareDay(date(2026, 9, 23), 20, 12)
    today = VersionShareDay(date(2026, 9, 24), 3, 2)
    report = StatsReport(days=7, new_from_build=10, share_day=yesterday.day)
    report.share_series = [yesterday, today]
    assert report.display_share is yesterday


@pytest.mark.asyncio
async def test_dashboard_queries_only_sections_visible_to_support() -> None:
    class Session:
        def __init__(self) -> None:
            self.queries: list[Any] = []

        async def scalar(self, stmt: Any) -> int:
            self.queries.append(stmt)
            return 2

    session = Session()
    report = await build_dashboard(session, permissions=frozenset({"support.read"}))  # type: ignore[arg-type]
    assert report.counts == {"support_awaiting": 2, "support_open": 2}
    assert report.stats is None
    assert len(session.queries) == 2


@pytest.mark.asyncio
async def test_revoked_permission_takes_effect_on_next_request() -> None:
    principal_id = uuid4()
    grants = ["support.read", "support.write"]

    class Result:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def scalars(self) -> Result:
            return self

        def tuples(self) -> Result:
            return self

        def all(self) -> list[Any]:
            return self.rows

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, *_args: Any) -> Any:
            return type("Principal", (), {"is_active": True})()

        async def execute(self, _stmt: Any) -> Result:
            self.calls += 1
            if self.calls == 1:
                return Result(["support"])
            if self.calls == 2:
                return Result(grants)
            return Result([])

    auth = AdminAuthBackend(
        secret_key="test-session-secret",
        session_factory=Session,  # type: ignore[arg-type]
        settings=Settings(app_env="test"),
    )
    cookie = {"admin_principal_id": str(principal_id), "admin_roles": ["support"]}
    first = Request({"type": "http", "session": cookie})  # type: ignore[arg-type]
    assert await auth.authenticate(first) is True
    assert require_permission(first, "support.write") is True

    grants.remove("support.write")
    second = Request({"type": "http", "session": cookie})  # type: ignore[arg-type]
    assert await auth.authenticate(second) is True
    assert require_permission(second, "support.write") is False


@pytest.mark.asyncio
async def test_exposed_post_requires_write_permission_even_with_direct_url() -> None:
    class Backend:
        permissions = frozenset({"support.read"})

        async def authenticate(self, request: Request) -> bool:
            request.state.admin_permissions = self.permissions
            return True

    class View(BaseView):
        category = "Поддержка"
        calls = 0

        def is_accessible(self, request: Request) -> bool:
            return require_permission(request, "support.read")

        @expose("/example", methods=["GET", "POST"])
        async def example(self, request: Request) -> Response:
            self.calls += 1
            return Response(status_code=200)

    admin = object.__new__(PermissionedAdmin)
    backend = Backend()
    admin.authentication_backend = backend  # type: ignore[assignment]
    view = View()
    protected = admin._protected(view.example, view, write=None)

    get_request = Request({"type": "http", "method": "GET", "session": {}})  # type: ignore[arg-type]
    assert (await protected(get_request)).status_code == 200
    post_request = Request({"type": "http", "method": "POST", "session": {}})  # type: ignore[arg-type]
    assert (await protected(post_request)).status_code == 403
    assert view.calls == 1

    backend.permissions = frozenset({"support.read", "support.write"})
    allowed_post = Request({"type": "http", "method": "POST", "session": {}})  # type: ignore[arg-type]
    assert (await protected(allowed_post)).status_code == 200
    assert view.calls == 2


@pytest.mark.asyncio
async def test_role_form_contains_employee_picker() -> None:
    principal_id = uuid4()

    class Rows:
        def tuples(self) -> Rows:
            return self

        def all(self) -> list[tuple[Any, str]]:
            return [(principal_id, "operator")]

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> Rows:
            return Rows()

    view = AdminRoleBindingAdmin()
    view.session_maker = Session  # type: ignore[assignment]
    form = (await view.scaffold_form())()
    assert form.principal_id.choices == [(str(principal_id), "operator")]
    assert form.role.choices


@pytest.mark.asyncio
async def test_get_action_requires_same_origin_navigation() -> None:
    class Backend:
        async def authenticate(self, request: Request) -> bool:
            request.state.admin_permissions = frozenset({"support.read", "support.write"})
            return True

    class View(BaseView):
        category = "Поддержка"
        calls = 0

        def is_accessible(self, request: Request) -> bool:
            return require_permission(request, "support.read")

        @expose("/mutation", methods=["GET"])
        async def mutation(self, request: Request) -> Response:
            self.calls += 1
            return Response(status_code=200)

    admin = object.__new__(PermissionedAdmin)
    admin.authentication_backend = Backend()  # type: ignore[assignment]
    view = View()
    action = admin._protected(view.mutation, view, write=True)
    naked = Request(  # type: ignore[arg-type]
        {
            "type": "http",
            "method": "GET",
            "session": {},
            "headers": [(b"host", b"admin.example")],
        }
    )
    assert (await action(naked)).status_code == 403
    allowed = Request(  # type: ignore[arg-type]
        {
            "type": "http",
            "method": "GET",
            "session": {},
            "headers": [(b"host", b"admin.example"), (b"sec-fetch-site", b"same-origin")],
        }
    )
    assert (await action(allowed)).status_code == 200
    assert view.calls == 1
