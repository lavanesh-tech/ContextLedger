from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.config import Environment


class HealthStatus(StrEnum):
    OK = "ok"


class HealthResponse(BaseModel):
    """Response body of ``GET /api/v1/health`` (liveness)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: HealthStatus
    service: str
    version: str
    environment: Environment
    timestamp: AwareDatetime


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class DependencyStatus(StrEnum):
    UP = "up"
    DOWN = "down"


class DatabaseReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DependencyStatus
    latency_ms: float = Field(ge=0)
    error: str | None = Field(
        default=None, description="Exception type only; details are in the server logs."
    )


class MigrationReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_revision: str | None
    expected_revision: str | None
    up_to_date: bool


class ReadinessResponse(BaseModel):
    """Response body of ``GET /api/v1/health/ready`` (200 when ready, 503 otherwise)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ReadinessStatus
    database: DatabaseReadiness
    migrations: MigrationReadiness
    timestamp: AwareDatetime
