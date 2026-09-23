from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.core.config import Environment


class HealthStatus(StrEnum):
    OK = "ok"


class HealthResponse(BaseModel):
    """Response body of ``GET /api/v1/health``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: HealthStatus
    service: str
    version: str
    environment: Environment
    timestamp: AwareDatetime
