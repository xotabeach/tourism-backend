from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.identity.application.tokens import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state,
        "session_factory",
        None,
    )
    if session_factory is None:
        raise RuntimeError("database session factory is not configured")

    async with session_factory() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError("settings are not configured")
    return settings


def get_redis(request: Request) -> Redis:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError("redis is not configured")
    return redis


async def get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(
            code="unauthorized",
            message="Authentication required",
            status_code=401,
        )
    settings = get_settings_dep(request)
    try:
        return decode_access_token(credentials.credentials, settings=settings)
    except (jwt.PyJWTError, ValueError, TypeError):
        raise AppError(
            code="unauthorized",
            message="Authentication required",
            status_code=401,
        ) from None


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RedisClient = Annotated[Redis, Depends(get_redis)]
CurrentUserId = Annotated[UUID, Depends(get_current_user_id)]
