"""Typed application configuration loaded from environment variables.

All settings use the ``CONTEXTLEDGER_`` prefix, e.g. ``CONTEXTLEDGER_LOG_LEVEL=DEBUG``.
Values are validated once at startup so a misconfigured deployment fails fast
instead of failing on the first request that happens to touch a bad value.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_V1_PREFIX: Final = "/api/v1"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    """Deployment environment the process is running in."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process-wide settings. Instances are immutable once created."""

    model_config = SettingsConfigDict(
        env_prefix="CONTEXTLEDGER_",
        # Later files win: backend/.env overrides the repository-root .env.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        # The shared .env also holds Docker Compose variables (POSTGRES_*, ...).
        extra="ignore",
        frozen=True,
    )

    app_name: str = Field(default="ContextLedger", min_length=1, max_length=100)
    environment: Environment = Environment.LOCAL
    log_level: LogLevel = "INFO"
    log_json: bool = True
    docs_enabled: bool = True
    correlation_id_header: str = Field(
        default="X-Correlation-ID",
        pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
