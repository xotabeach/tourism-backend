from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: str = "postgresql+asyncpg://tourism:local-tourism-password@localhost:5432/tourism"
    database_url_sync: str = (
        "postgresql+psycopg://tourism:local-tourism-password@localhost:5432/tourism"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
