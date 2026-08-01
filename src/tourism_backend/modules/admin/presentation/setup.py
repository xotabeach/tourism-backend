"""Mount SQLAdmin on the FastAPI app."""

from __future__ import annotations

from fastapi import FastAPI
from sqladmin import Admin
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware import Middleware

from tourism_backend.config import Settings
from tourism_backend.modules.admin.presentation.auth import AdminAuthBackend
from tourism_backend.modules.admin.presentation.csrf import AdminCsrfMiddleware
from tourism_backend.modules.admin.presentation.views import register_views


def mount_admin(
    app: FastAPI,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Admin | None:
    if not settings.admin_enabled:
        return None

    auth = AdminAuthBackend(
        secret_key=settings.admin_session_secret,
        session_factory=session_factory,
        settings=settings,
        same_site="lax",
        https_only=settings.app_env.value in {"staging", "production"},
    )
    admin = Admin(
        app=app,
        session_maker=session_factory,
        base_url="/admin",
        title="CrimeaTrip Ops",
        authentication_backend=auth,
        middlewares=[Middleware(AdminCsrfMiddleware, path_prefix="/admin")],
    )
    register_views(admin, settings)
    app.state.admin = admin
    return admin
