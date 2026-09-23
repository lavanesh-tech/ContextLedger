"""Fixtures for PostgreSQL-backed tests.

Run with ``make test`` while ``make up`` is running. The session fixture drops
and recreates ``contextledger_test`` and migrates it to head once per run.
Without a reachable database these tests are skipped locally and fail in CI.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.main import create_app
from tests.integration.support import (
    get_test_database_url,
    recreate_database,
    run_alembic,
    settings_for,
    unavailable,
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_database() -> URL:
    url = get_test_database_url()
    try:
        await recreate_database(url)
    except Exception as exc:  # connection refused, bad password, ...
        unavailable(f"PostgreSQL is not reachable ({type(exc).__name__}); start it with `make up`")

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await run_alembic(engine, "upgrade", "head")
    finally:
        await engine.dispose()
    return url


@pytest.fixture
async def engine(migrated_database: URL) -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def db_app(migrated_database: URL) -> AsyncIterator[FastAPI]:
    app = create_app(settings_for(migrated_database))
    yield app
    await app.state.db_engine.dispose()


@pytest.fixture
async def db_client(db_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=db_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
