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
    # Groq model with JSON-schema structured outputs; final choice follows the eval baseline.
    llm_fallback_model: str = "groq/openai/gpt-oss-120b"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    llm_timeout_seconds: float = 90.0
    llm_max_output_tokens: int = 8192
    # Input caps (tokens) even when the window is larger: latency and cost (M2 plan §3.3).
    llm_input_cap_primary: int = 150_000
    llm_input_cap_fallback: int = 90_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
