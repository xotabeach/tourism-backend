import os
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
from tourism_backend.modules.admin.application.bootstrap import ensure_bootstrap_admin
from tourism_backend.modules.admin.presentation.setup import mount_admin

# Prefer explicit MEDIA_ROOT (container: /app/data/media). Fallback walks from
# source layout `src/tourism_backend/main.py` → repo root / data / media.
_MEDIA_DIR = Path(
    os.environ.get(
        "MEDIA_ROOT",
        str(Path(__file__).resolve().parents[2] / "data" / "media"),
    )
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from sqlalchemy.exc import SQLAlchemyError

    settings: Settings = app.state.settings
    try:
        await ensure_bootstrap_admin(app.state.session_factory, settings)
    except SQLAlchemyError:
        # Unit tests may construct the app without a live database.
        if settings.app_env not in {AppEnvironment.LOCAL, AppEnvironment.TEST}:
            raise
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    if settings is not None:
        from tourism_backend.config import validate_settings

        validate_settings(resolved_settings)
    configure_logging(resolved_settings)

    engine = create_engine(resolved_settings)
    session_factory = create_session_factory(engine)
    redis_client = create_redis_client(resolved_settings)

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    register_exception_handlers(app)
    app.include_router(api_router)
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")
    mount_admin(app, session_factory=session_factory, settings=resolved_settings)
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
