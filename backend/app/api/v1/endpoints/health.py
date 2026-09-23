"""Liveness and readiness endpoints.

* ``GET /api/v1/health`` (liveness) answers "is the process up?". It never
  touches a dependency, so a database outage does not make an orchestrator
  restart healthy API containers.
* ``GET /api/v1/health/ready`` (readiness) answers "can this instance serve
  traffic?". It checks PostgreSQL and the migration state and returns 503 when
  the answer is no, so traffic is routed elsewhere until it recovers.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.dependencies import EngineDep, ExpectedRevisionDep, SettingsDep
from app.schemas.health import (
    DatabaseReadiness,
    DependencyStatus,
    HealthResponse,
    HealthStatus,
    MigrationReadiness,
    ReadinessResponse,
    ReadinessStatus,
)
from app.services.readiness import ReadinessReport, check_readiness

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness check", response_model=HealthResponse)
async def get_health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/health/ready",
    summary="Readiness check",
    response_model=ReadinessResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "PostgreSQL is unreachable or the schema has never been migrated.",
        }
    },
)
async def get_readiness(
    response: Response,
    engine: EngineDep,
    settings: SettingsDep,
    expected_revision: ExpectedRevisionDep,
) -> ReadinessResponse:
    report = await check_readiness(
        engine,
        expected_revision=expected_revision,
        timeout_seconds=settings.readiness_timeout_seconds,
    )
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return _to_response(report)


def _to_response(report: ReadinessReport) -> ReadinessResponse:
    db = report.database
    return ReadinessResponse(
        status=ReadinessStatus.READY if report.ready else ReadinessStatus.NOT_READY,
        database=DatabaseReadiness(
            status=DependencyStatus.UP if db.reachable else DependencyStatus.DOWN,
            latency_ms=db.latency_ms,
            error=db.error,
        ),
        migrations=MigrationReadiness(
            current_revision=db.current_revision,
            expected_revision=report.expected_revision,
            up_to_date=report.schema_up_to_date,
        ),
        timestamp=datetime.now(UTC),
    )
