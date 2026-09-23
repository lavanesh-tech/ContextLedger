"""Readiness against a real PostgreSQL."""

import pytest
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services.readiness import check_readiness
from tests.integration.support import alembic_config

pytestmark = pytest.mark.integration


async def test_ready_when_database_is_reachable_and_migrated(db_client: AsyncClient) -> None:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()

    response = await db_client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"]["status"] == "up"
    assert body["database"]["error"] is None
    assert body["database"]["latency_ms"] >= 0
    assert body["migrations"] == {
        "current_revision": head,
        "expected_revision": head,
        "up_to_date": True,
    }


async def test_not_ready_when_database_was_never_migrated(migrated_database: URL) -> None:
    # The built-in "postgres" database is reachable but has no alembic_version table.
    engine = create_async_engine(migrated_database.set(database="postgres"), poolclass=NullPool)
    try:
        report = await check_readiness(engine, expected_revision="0001", timeout_seconds=5)
    finally:
        await engine.dispose()

    assert report.database.reachable is True
    assert report.database.current_revision is None
    assert report.ready is False
    assert report.schema_up_to_date is False


async def test_ready_but_flagged_when_schema_is_behind(migrated_database: URL) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        report = await check_readiness(engine, expected_revision="9999", timeout_seconds=5)
    finally:
        await engine.dispose()

    # Expand/contract migrations keep older code compatible, so a revision
    # mismatch is reported but does not take instances out of rotation.
    assert report.ready is True
    assert report.schema_up_to_date is False
