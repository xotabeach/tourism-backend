from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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
_ADMIN_SESSION_PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "local-admin-session",
)


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AIProvider(StrEnum):
    MOCK = "mock"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"


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
    # None = auto (True for local only). Explicit True/False overrides.
    # Never allowed outside local/test — see validate_settings.
    auth_otp_accept_any: bool | None = None
    # Keeps the generated OTP readable in the database while no SMS provider is
    # connected. None = auto (True for local/test). Refused in staging/production.
    auth_otp_store_debug_code: bool | None = None

    # Phase 6.5 ops admin (SQLAdmin). Session secret is separate from JWT.
    admin_enabled: bool = True
    admin_session_secret: str = "local-admin-session-secret-dev-only-not-for-prod"
    admin_bootstrap_login: str | None = None
    admin_bootstrap_password: str | None = None

    # FCM HTTP v1 (optional). Leave empty → in-app only, no system push.
    fcm_service_account_json: str | None = None
    fcm_service_account_file: str | None = None

    # Phase 8B provider transport. Disabled until the deterministic builder
    # and domain validation pipeline are ready.
    ai_planning_enabled: bool = False
    ai_provider: AIProvider = AIProvider.MOCK
    ai_model: str | None = None
    ai_request_timeout_seconds: float = Field(default=60, ge=1, le=300)
    ai_max_repair_attempts: int = Field(default=1, ge=0, le=2)
    ai_prompt_version: str = "v1"
    lm_studio_base_url: str | None = None
    lm_studio_model: str | None = None
    lm_studio_api_key: SecretStr | None = None
    rag_enabled: bool = False
    rag_top_k: int = Field(default=4, ge=1, le=8)
    rag_embedding_model: str = "hash-v1"

    # ADR-004 RoutingProvider. stub = synthetic haversine; osrm = later.
    routing_provider: Literal["stub"] = "stub"
    osrm_base_url: str | None = None
    routing_timeout_seconds: float = Field(default=10, ge=1, le=60)

    @property
    def otp_accept_any_enabled(self) -> bool:
        if self.auth_otp_accept_any is not None:
            return self.auth_otp_accept_any
        return self.app_env is AppEnvironment.LOCAL

    @property
    def otp_store_debug_code_enabled(self) -> bool:
        if self.auth_otp_store_debug_code is not None:
            return self.auth_otp_store_debug_code
        return self.app_env in {AppEnvironment.LOCAL, AppEnvironment.TEST}


def validate_settings(settings: Settings) -> None:
    """Refuse local-only auth shortcuts and placeholder credentials."""
    if settings.ai_planning_enabled and settings.ai_provider is AIProvider.LMSTUDIO:
        if not settings.lm_studio_base_url or not settings.lm_studio_model:
            raise RuntimeError(
                "LM_STUDIO_BASE_URL and LM_STUDIO_MODEL are required when "
                "AI_PROVIDER=lmstudio and AI_PLANNING_ENABLED=true"
            )
        if not settings.lm_studio_base_url.startswith(("http://", "https://")):
            raise RuntimeError("LM_STUDIO_BASE_URL must use http:// or https://")

    if settings.app_env in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}:
        if settings.otp_accept_any_enabled:
            msg = (
                "Refusing to start: AUTH_OTP_ACCEPT_ANY accepts any OTP code and "
                f"is not allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)
        if settings.otp_store_debug_code_enabled:
            msg = (
                "Refusing to start: AUTH_OTP_STORE_DEBUG_CODE stores OTP codes in "
                f"cleartext and is not allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)

    if settings.app_env in {AppEnvironment.LOCAL, AppEnvironment.TEST}:
        return
    blob = " ".join(
        (
            settings.database_url,
            settings.database_url_sync,
            settings.redis_url,
            settings.jwt_signing_key,
            settings.admin_session_secret,
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
    for marker in _ADMIN_SESSION_PLACEHOLDER_MARKERS:
        if marker in settings.admin_session_secret.lower():
            msg = (
                "Refusing to start: placeholder admin session secret is not "
                f"allowed when app_env={settings.app_env.value!r}"
            )
            raise RuntimeError(msg)
    if len(settings.admin_session_secret) < 32:
        raise RuntimeError("Admin session secret must be at least 32 characters outside local/test")
    if settings.admin_bootstrap_password and len(settings.admin_bootstrap_password) < 12:
        raise RuntimeError(
            "ADMIN_BOOTSTRAP_PASSWORD must be at least 12 characters outside local/test"
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    validate_settings(settings)
    return settings
