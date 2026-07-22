from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(tags=["health"])


async def _check_dependencies(request: Request) -> JSONResponse | None:
    session_factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state,
        "session_factory",
        None,
    )
    if session_factory is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "detail": "database session factory is missing"},
        )

    redis_client: Redis | None = getattr(request.app.state, "redis", None)
    if redis_client is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "detail": "redis client is missing"},
        )

    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - readiness probe reports dependency errors
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "detail": f"database: {exc}"},
        )

    try:
        await redis_client.ping()
    except Exception as exc:  # noqa: BLE001 - readiness probe reports dependency errors
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "detail": f"redis: {exc}"},
        )

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
