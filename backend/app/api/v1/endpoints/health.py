"""Liveness endpoint.

This is a *liveness* check: it answers "is the process up and serving HTTP?"
and deliberately does not touch PostgreSQL, Redis, Neo4j or Kafka. A separate
*readiness* check that verifies dependencies is added in Phase 2, so that a
database blip does not cause an orchestrator to restart healthy API pods.
"""

from datetime import UTC, datetime

from fastapi import APIRouter

from app import __version__
from app.api.dependencies import SettingsDep
from app.schemas.health import HealthResponse, HealthStatus

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness check",
    response_model=HealthResponse,
)
async def get_health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
    )
