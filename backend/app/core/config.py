"""Typed application configuration loaded from environment variables.

All settings use the ``CONTEXTLEDGER_`` prefix, e.g. ``CONTEXTLEDGER_LOG_LEVEL=DEBUG``.
Values are validated once at startup so a misconfigured deployment fails fast
instead of failing on the first request that happens to touch a bad value.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

API_V1_PREFIX: Final = "/api/v1"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
EmbeddingProviderName = Literal["deterministic", "openai"]
PrivacyScopeName = Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]


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
        # "KEY=" in a .env file means "not set", not "set to an empty string".
        env_ignore_empty=True,
        frozen=True,
    )

    # --- Application ---------------------------------------------------------
    app_name: str = Field(default="ContextLedger", min_length=1, max_length=100)
    environment: Environment = Environment.LOCAL
    log_level: LogLevel = "INFO"
    log_json: bool = True
    docs_enabled: bool = True
    correlation_id_header: str = Field(
        default="X-Correlation-ID",
        pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$",
    )

    # --- PostgreSQL (system of record) ---------------------------------------
    # Separate fields rather than one DSN string: they map 1:1 onto the JSON
    # secret AWS RDS stores in Secrets Manager, and URL.create() escapes
    # special characters in passwords correctly.
    db_host: str = Field(default="localhost", min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_user: str = Field(default="contextledger", min_length=1)
    db_password: SecretStr = SecretStr("")
    db_name: str = Field(default="contextledger", min_length=1, max_length=63)
    db_application_name: str = Field(default="contextledger-api", min_length=1, max_length=63)

    # Connection pool. Sized for one API process; tuned with measurements later.
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    # Server-side cap on any single statement, so one bad query cannot hold a
    # connection (and a pool slot) forever.
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)
    db_echo: bool = False

    # --- Embeddings -------------------------------------------------------------
    # "deterministic" (default) needs no network or API key: a hashing embedder
    # for local development, CI and demos. It is lexical, not semantic.
    # "openai" calls the OpenAI embeddings API and needs CONTEXTLEDGER_OPENAI_API_KEY.
    embedding_provider: EmbeddingProviderName = "deterministic"
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1, max_length=100)
    embedding_batch_size: int = Field(default=64, ge=1, le=512)
    embedding_max_attempts: int = Field(default=5, ge=1, le=20)
    embedding_retry_base_seconds: float = Field(default=10.0, ge=0)
    embedding_retry_cap_seconds: float = Field(default=900.0, gt=0)
    embedding_lease_seconds: int = Field(default=300, ge=10)
    embedding_poll_interval_seconds: float = Field(default=5.0, gt=0)
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    openai_timeout_seconds: float = Field(default=30.0, gt=0)
    openai_max_retries: int = Field(default=3, ge=0, le=10)

    # --- Neo4j (provenance graph, a projection of PostgreSQL) -------------------
    neo4j_uri: str = Field(default="bolt://localhost:7687", pattern=r"^(bolt|neo4j)(\+s|\+ssc)?://")
    neo4j_user: str = Field(default="neo4j", min_length=1)
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = Field(default="neo4j", min_length=1, max_length=63)
    graph_batch_size: int = Field(default=200, ge=1, le=5000)
    graph_poll_interval_seconds: float = Field(default=2.0, gt=0)

    # --- MCP server (stdio) -----------------------------------------------------
    # The principal the local MCP server acts as. Tools never accept a tenant or
    # user from the model; membership is re-checked on every call. OAuth for the
    # HTTP transport arrives in Phase 13.
    mcp_user_id: UUID | None = None
    mcp_organization_id: UUID | None = None
    mcp_agent_name: str = Field(default="mcp-agent", min_length=1, max_length=200)
    # Most sensitive privacy scope this agent may ever read (narrows the user's role).
    mcp_max_privacy_scope: PrivacyScopeName = "INTERNAL"

    # --- Health / migrations ---------------------------------------------------
    readiness_timeout_seconds: float = Field(default=3.0, gt=0)
    alembic_ini_path: Path = Path("alembic.ini")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_db_password_outside_local(self) -> Self:
        deployed = self.environment in {Environment.STAGING, Environment.PRODUCTION}
        if deployed and not self.db_password.get_secret_value():
            raise ValueError("CONTEXTLEDGER_DB_PASSWORD must be set in staging and production")
        return self

    @model_validator(mode="after")
    def _require_real_embeddings_when_configured(self) -> Self:
        if self.embedding_provider == "openai" and not self.openai_api_key.get_secret_value():
            raise ValueError("CONTEXTLEDGER_OPENAI_API_KEY is required when the provider is openai")
        deployed = self.environment in {Environment.STAGING, Environment.PRODUCTION}
        if deployed and self.embedding_provider != "openai":
            raise ValueError("staging and production must use the openai embedding provider")
        return self

    @model_validator(mode="after")
    def _require_neo4j_password_outside_local(self) -> Self:
        deployed = self.environment in {Environment.STAGING, Environment.PRODUCTION}
        if deployed and not self.neo4j_password.get_secret_value():
            raise ValueError("CONTEXTLEDGER_NEO4J_PASSWORD must be set in staging and production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL for the async (asyncpg) driver. Never log this object's string form."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value() or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
