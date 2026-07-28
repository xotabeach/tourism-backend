from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from tourism_backend.api.errors import register_exception_handlers
from tourism_backend.api.router import api_router
from tourism_backend.config import AppEnvironment, Settings, get_settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.logging_config import configure_logging

# src/tourism_backend/main.py → repo root / data / media
_MEDIA_DIR = Path(__file__).resolve().parents[2] / "data" / "media"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    redis_client = create_redis_client(settings)
    app.state.redis = redis_client
    try:
        yield
    finally:
        await redis_client.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    if settings is not None:
        from tourism_backend.config import validate_settings

        validate_settings(resolved_settings)
    configure_logging(resolved_settings)

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    register_exception_handlers(app)
    app.include_router(api_router)
    if _MEDIA_DIR.is_dir():
        app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "tourism_backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.app_env is AppEnvironment.LOCAL,
    )


if __name__ == "__main__":
    main()
