from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_PLACEHOLDER_MARKERS = (
    "local-tourism-password",
    "local-minio-password",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "tourism-backend"
    environment: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    # Local DX defaults only. Staging/production must override via env
    # (see validate_settings / refuse placeholder credentials).
    database_url: str = "postgresql+asyncpg://tourism:local-tourism-password@localhost:5432/tourism"
    database_url_sync: str = (
        "postgresql+psycopg://tourism:local-tourism-password@localhost:5432/tourism"
    )
    redis_url: str = "redis://localhost:6379/0"


def validate_settings(settings: Settings) -> None:
    """Refuse known local placeholder credentials outside development."""
    if settings.environment.lower() in {"development", "test", "ci"}:
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
                f"allowed when environment={settings.environment!r}"
            )
            raise RuntimeError(msg)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    validate_settings(settings)
    return settings
