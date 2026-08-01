"""Upsert bootstrap admin principal from env (local/test DX)."""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tourism_backend.config import AppEnvironment, Settings
from tourism_backend.modules.admin.application.passwords import hash_password
from tourism_backend.modules.admin.infrastructure.models import (
    AdminPrincipal,
    AdminRoleBinding,
)

logger = logging.getLogger(__name__)


async def ensure_bootstrap_admin(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    login = (settings.admin_bootstrap_login or "").strip()
    password = settings.admin_bootstrap_password or ""
    if not login or not password:
        return
    # Staging/prod may still bootstrap explicitly, but never with a short password.
    if settings.app_env not in {AppEnvironment.LOCAL, AppEnvironment.TEST} and len(password) < 12:
        logger.warning("Skipping admin bootstrap: password too short outside local/test")
        return

    async with session_factory() as session:
        result = await session.execute(select(AdminPrincipal).where(AdminPrincipal.login == login))
        principal = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if principal is None:
            principal = AdminPrincipal(
                id=uuid4(),
                login=login,
                password_hash=hash_password(password),
                is_active=True,
                created_at=now,
                updated_at=now,
            )
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
            await session.commit()
            logger.info("Bootstrapped admin principal login=%s", login)
            return

        # Refresh password hash so local DX env changes take effect.
        principal.password_hash = hash_password(password)
        principal.is_active = True
        principal.updated_at = now
        roles = await session.execute(
            select(AdminRoleBinding).where(AdminRoleBinding.principal_id == principal.id)
        )
        if not list(roles.scalars().all()):
            session.add(
                AdminRoleBinding(
                    id=uuid4(),
                    principal_id=principal.id,
                    role="admin",
                    created_at=now,
                )
            )
        await session.commit()
