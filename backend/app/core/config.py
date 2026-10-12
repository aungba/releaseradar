from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables prefixed with ``RR_``."""

    model_config = SettingsConfigDict(env_prefix="RR_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://releaseradar:releaseradar@localhost:5432/releaseradar"
    redis_url: str = "redis://localhost:6379/0"
    # Fernet key used to encrypt provider access tokens and webhook secrets at rest.
    encryption_key: str = ""
    cors_origins: list[str] = ["http://localhost:5173"]

    llm_primary_model: str = "gemini/gemini-2.5-pro"
    llm_fallback_model: str = "groq/llama-3.3-70b-versatile"


@lru_cache
def get_settings() -> Settings:
    return Settings()
