"""Fixtures for PostgreSQL-backed tests.

Run with ``make test`` while ``make up`` is running. The session fixture drops
and recreates ``contextledger_test`` and migrates it to head once per run.
Without a reachable database these tests are skipped locally and fail in CI.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from neo4j import AsyncDriver, AsyncGraphDatabase
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.main import create_app
from app.provenance.graph import GraphReader, GraphWriter, ensure_schema
from app.workers.graph import GraphProjector
from tests.integration.support import (
    get_test_database_url,
    get_test_neo4j,
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


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on a NullPool engine: every session gets its own real connection."""
    return async_sessionmaker(engine, expire_on_commit=False)


NEO4J_DATABASE = "neo4j"


@dataclass
class GraphHarness:
    """Neo4j access for tests. Nodes of every tracked organization are deleted afterwards."""

    driver: AsyncDriver
    database: str = NEO4J_DATABASE
    organizations: set[UUID] = field(default_factory=set)

    @property
    def writer(self) -> GraphWriter:
        return GraphWriter(self.driver, self.database)

    @property
    def reader(self) -> GraphReader:
        return GraphReader(self.driver, self.database)

    def projector(
        self, sessions: async_sessionmaker[AsyncSession], organization_id: UUID
    ) -> GraphProjector:
        self.organizations.add(organization_id)
        return GraphProjector(sessions, self.writer, organization_id=organization_id)

    async def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        async with self.driver.session(database=self.database) as session:
            result = await session.run(cypher, **params)
            return [record.data() for record in await result.fetch(10_000)]


@pytest.fixture
async def graph() -> AsyncIterator[GraphHarness]:
    uri, user, password = get_test_neo4j()
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # connection refused, bad credentials, ...
        await driver.close()
        unavailable(f"Neo4j is not reachable ({type(exc).__name__}); start it with `make up`")
    await ensure_schema(driver, NEO4J_DATABASE)
    harness = GraphHarness(driver)
    try:
        yield harness
    finally:
        for organization_id in harness.organizations:
            await harness.query("MATCH (n {org: $org}) DETACH DELETE n", org=str(organization_id))
        await driver.close()
