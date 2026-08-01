"""SQLAdmin authentication — cookie session, never mobile JWT."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse

from tourism_backend.config import Settings
from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.application.passwords import verify_password
from tourism_backend.modules.admin.infrastructure.models import (
    AdminPrincipal,
    AdminRoleBinding,
)

_SESSION_PRINCIPAL = "admin_principal_id"
_SESSION_ROLES = "admin_roles"
_RATE_WINDOW_SEC = 600
_RATE_LOGIN_LIMIT = 20


class AdminAuthBackend(AuthenticationBackend):
    def __init__(
        self,
        secret_key: str,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        **session_kwargs: Any,
    ) -> None:
        super().__init__(secret_key, **session_kwargs)
        self._session_factory = session_factory
        self._settings = settings

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        if not username or not password:
            return False

        redis: Redis | None = getattr(request.app.state, "redis", None)
        client_ip = request.client.host if request.client else "unknown"
        if redis is not None:
            key = f"admin:login:ip:{client_ip}"
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, _RATE_WINDOW_SEC)
            if count > _RATE_LOGIN_LIMIT:
                return False

        async with self._session_factory() as session:
            result = await session.execute(
                select(AdminPrincipal).where(AdminPrincipal.login == username)
            )
            principal = result.scalar_one_or_none()
            if (
                principal is None
                or not principal.is_active
                or not verify_password(password, principal.password_hash)
            ):
                await record_audit(
                    session,
                    actor_id=None,
                    action="admin.login_failed",
                    entity_type="admin_principal",
                    entity_id=username[:64],
                    ip=client_ip,
                    commit=True,
                )
                return False

            roles_result = await session.execute(
                select(AdminRoleBinding.role).where(
                    AdminRoleBinding.principal_id == principal.id
                )
            )
            roles = list(roles_result.scalars().all())
            if not roles:
                return False

            request.session[_SESSION_PRINCIPAL] = str(principal.id)
            request.session[_SESSION_ROLES] = roles
            await record_audit(
                session,
                actor_id=principal.id,
                action="admin.login",
                entity_type="admin_principal",
                entity_id=str(principal.id),
                ip=client_ip,
                commit=True,
            )
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        raw_id = request.session.get(_SESSION_PRINCIPAL)
        if not raw_id:
            return False
        try:
            principal_id = UUID(str(raw_id))
        except ValueError:
            request.session.clear()
            return False

        async with self._session_factory() as session:
            principal = await session.get(AdminPrincipal, principal_id)
            if principal is None or not principal.is_active:
                request.session.clear()
                return False
        return True


def session_principal_id(request: Request) -> UUID | None:
    raw = request.session.get(_SESSION_PRINCIPAL)
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def session_roles(request: Request) -> list[str]:
    roles = request.session.get(_SESSION_ROLES) or []
    if isinstance(roles, list):
        return [str(r) for r in roles]
    return []


def require_admin_role(request: Request) -> bool:
    return "admin" in session_roles(request)


async def redirect_login(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("admin:login"), status_code=302)
