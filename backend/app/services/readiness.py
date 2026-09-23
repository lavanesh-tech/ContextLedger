"""Readiness: can this process serve real traffic right now?

Liveness (``GET /api/v1/health``) only proves the process is up. Readiness
additionally proves PostgreSQL is reachable and has been migrated, so a load
balancer or Kubernetes stops routing traffic to an instance that would fail
every database request, without restarting it.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger("contextledger.readiness")


@dataclass(frozen=True, slots=True)
class DatabaseProbe:
    reachable: bool
    latency_ms: float
    current_revision: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    database: DatabaseProbe
    expected_revision: str | None

    @property
    def ready(self) -> bool:
        # A reachable but never-migrated database cannot serve requests.
        return self.database.reachable and self.database.current_revision is not None

    @property
    def schema_up_to_date(self) -> bool:
        current = self.database.current_revision
        return current is not None and current == self.expected_revision


async def _current_revision(connection: AsyncConnection) -> str | None:
    table_exists = await connection.scalar(
        text("SELECT to_regclass('alembic_version') IS NOT NULL")
    )
    if not table_exists:
        return None
    result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    revision = result.scalars().first()
    return None if revision is None else str(revision)


async def probe_database(engine: AsyncEngine, timeout_seconds: float) -> DatabaseProbe:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds), engine.connect() as connection:
            revision = await _current_revision(connection)
    except Exception as exc:  # any failure means "not ready"; never crash the probe
        logger.warning(
            "readiness.database_unavailable",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
        )
        return DatabaseProbe(
            reachable=False,
            latency_ms=_elapsed_ms(started),
            current_revision=None,
            # Only the exception type is exposed to callers: messages can
            # contain hostnames and other infrastructure details.
            error=type(exc).__name__,
        )
    return DatabaseProbe(reachable=True, latency_ms=_elapsed_ms(started), current_revision=revision)


async def check_readiness(
    engine: AsyncEngine, *, expected_revision: str | None, timeout_seconds: float
) -> ReadinessReport:
    return ReadinessReport(
        database=await probe_database(engine, timeout_seconds),
        expected_revision=expected_revision,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
