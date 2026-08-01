"""Mount SQLAdmin on the FastAPI app."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqladmin import Admin
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware import Middleware

from tourism_backend.config import AppEnvironment, Settings
from tourism_backend.modules.admin.presentation.auth import AdminAuthBackend
from tourism_backend.modules.admin.presentation.csrf import AdminCsrfMiddleware
from tourism_backend.modules.admin.presentation.views import register_views
from tourism_backend.modules.identity.infrastructure.models import User

_THEME_ROOT = Path(__file__).resolve().parents[1] / "theme"
_TEMPLATES_DIR = str(_THEME_ROOT / "templates")
_STATIC_DIR = _THEME_ROOT / "static"


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
        # Secure cookies on staging/prod. Test contour is HTTPS but CI clients use http://.
        https_only=settings.app_env in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION},
    )
    admin = Admin(
        app=app,
        session_maker=session_factory,
        base_url="/admin",
        title="КрымТрип Ops",
        authentication_backend=auth,
        templates_dir=_TEMPLATES_DIR,
        middlewares=[Middleware(AdminCsrfMiddleware, path_prefix="/admin")],
    )
    # Nested under /admin so templates can use url_for('admin:theme', ...).
    if not any(getattr(r, "name", None) == "theme" for r in admin.admin.routes):
        admin.admin.mount(
            "/theme",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="theme",
        )
    register_views(admin, settings)
    app.state.admin = admin

    @app.get("/admin/api/user-brief/{user_id}")
    async def admin_user_brief(user_id: UUID, request: Request) -> JSONResponse:
        if not await auth.authenticate(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        async with session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                return JSONResponse({"detail": "not_found"}, status_code=404)
            return JSONResponse(
                {
                    "id": str(user.id),
                    "display_name": user.display_name,
                    "phone_e164": user.phone_e164,
                }
            )

    return admin
