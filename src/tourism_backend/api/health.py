import asyncio
import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(tags=["health"])
logger = logging.getLogger("tourism_backend.health")

_DEPENDENCY_TIMEOUT_SECONDS = 2


def _not_ready(dependency: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "detail": f"{dependency} unavailable"},
    )


async def _check_dependencies(request: Request) -> JSONResponse | None:
    session_factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state,
        "session_factory",
        None,
    )
    if session_factory is None:
        return _not_ready("database")

    redis_client: Redis | None = getattr(request.app.state, "redis", None)
    if redis_client is None:
        return _not_ready("redis")

    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - dependency failures map to stable probe status
        logger.warning("readiness_database_unavailable", exc_info=exc)
        return _not_ready("database")

    try:
        async with asyncio.timeout(_DEPENDENCY_TIMEOUT_SECONDS):
            await redis_client.ping()
    except Exception as exc:  # noqa: BLE001 - dependency failures map to stable probe status
        logger.warning("readiness_redis_unavailable", exc_info=exc)
        return _not_ready("redis")

    return None


@router.get("/health/live")
@router.get("/health")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
@router.get("/ready")
async def health_ready(request: Request) -> JSONResponse:
    failure = await _check_dependencies(request)
    if failure is not None:
        return failure
    return JSONResponse(content={"status": "ready"})
