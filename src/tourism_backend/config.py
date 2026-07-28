from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_PLACEHOLDER_MARKERS = (
    "local-tourism-password",
    "local-minio-password",
)


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "tourism-backend"
    app_env: AppEnvironment = AppEnvironment.LOCAL
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    # Local DX defaults only. Non-local deployments must override via env
    # (see validate_settings / refuse placeholder credentials).
    database_url: str = "postgresql+asyncpg://tourism:local-tourism-password@localhost:5432/tourism"
    database_url_sync: str = (
        "postgresql+psycopg://tourism:local-tourism-password@localhost:5432/tourism"
    )
    redis_url: str = "redis://localhost:6379/0"


def validate_settings(settings: Settings) -> None:
    """Refuse known local placeholder credentials outside local/test."""
    if settings.app_env in {AppEnvironment.LOCAL, AppEnvironment.TEST}:
        return
    blob = " ".join(
        (
            settings.database_url,
            settings.database_url_sync,
            settings.redis_url,
        )
    )
    for marker in _LOCAL_PLACEHOLDER_MARKERS:
        if marker in blob:
            msg = (
                f"Refusing to start: placeholder credential {marker!r} is not "
                f"allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    validate_settings(settings)
    return settings
