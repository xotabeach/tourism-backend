from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_PLACEHOLDER_MARKERS = (
    "local-tourism-password",
    "local-minio-password",
)
_JWT_PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "local-jwt-signing-key",
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

    jwt_signing_key: str = "local-jwt-signing-key-dev-only-not-for-prod"
    jwt_issuer: str = "crimeatrip-local"
    jwt_audience: str = "crimeatrip-mobile"
    jwt_access_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    jwt_refresh_ttl_days: int = Field(default=30, ge=1, le=365)
    # None = auto (True for local/test). Explicit True/False overrides.
    auth_otp_accept_any: bool | None = None

    @property
    def otp_accept_any_enabled(self) -> bool:
        if self.auth_otp_accept_any is not None:
            return self.auth_otp_accept_any
        return self.app_env in {AppEnvironment.LOCAL, AppEnvironment.TEST}


def validate_settings(settings: Settings) -> None:
    """Refuse known local placeholder credentials outside local/test."""
    if settings.app_env in {AppEnvironment.LOCAL, AppEnvironment.TEST}:
        return
    blob = " ".join(
        (
            settings.database_url,
            settings.database_url_sync,
            settings.redis_url,
            settings.jwt_signing_key,
        )
    )
    for marker in _LOCAL_PLACEHOLDER_MARKERS:
        if marker in blob:
            msg = (
                f"Refusing to start: placeholder credential {marker!r} is not "
                f"allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)
    for marker in _JWT_PLACEHOLDER_MARKERS:
        if marker in settings.jwt_signing_key.lower():
            msg = (
                "Refusing to start: placeholder JWT signing key is not "
                f"allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)
    if len(settings.jwt_signing_key) < 32:
        raise RuntimeError("JWT signing key must be at least 32 characters outside local/test")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    validate_settings(settings)
    return settings
