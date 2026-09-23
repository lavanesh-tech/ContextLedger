"""Engine, session and transaction behaviour against a real PostgreSQL."""

from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.api.dependencies import SessionDep

pytestmark = pytest.mark.integration


def _sessions(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = app.state.db_sessionmaker
    return factory


async def _table_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as connection:
        exists = await connection.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name})
    return bool(exists)


async def test_session_factory_executes_queries(db_app: FastAPI) -> None:
    async with _sessions(db_app)() as session:
        assert await session.scalar(text("SELECT 1")) == 1


async def test_connection_settings_are_applied_server_side(db_app: FastAPI) -> None:
    async with db_app.state.db_engine.connect() as connection:
        assert await connection.scalar(text("SHOW application_name")) == "contextledger-api"
        assert await connection.scalar(text("SHOW statement_timeout")) == "30s"
        assert await connection.scalar(text("SHOW TimeZone")) is not None


async def test_failed_unit_of_work_is_rolled_back(db_app: FastAPI, engine: AsyncEngine) -> None:
    table = f"rollback_probe_{uuid4().hex[:12]}"

    async def failing_unit_of_work() -> None:
        async with _sessions(db_app)() as session, session.begin():
            await session.execute(text(f"CREATE TABLE {table} (id integer)"))
            raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        await failing_unit_of_work()

    assert not await _table_exists(engine, table)


async def test_committed_unit_of_work_is_persisted(db_app: FastAPI, engine: AsyncEngine) -> None:
    table = f"commit_probe_{uuid4().hex[:12]}"

    async with _sessions(db_app)() as session, session.begin():
        await session.execute(text(f"CREATE TABLE {table} (id integer)"))

    try:
        assert await _table_exists(engine, table)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))


async def test_session_without_commit_does_not_persist(
    db_app: FastAPI, engine: AsyncEngine
) -> None:
    table = f"uncommitted_probe_{uuid4().hex[:12]}"

    async with _sessions(db_app)() as session:
        await session.execute(text(f"CREATE TABLE {table} (id integer)"))
        # leaving the block closes the session without commit -> rollback

    assert not await _table_exists(engine, table)


async def test_request_scoped_session_dependency(db_app: FastAPI, db_client: AsyncClient) -> None:
    @db_app.get("/_test/session")
    async def probe(session: SessionDep) -> dict[str, int]:
        value = await session.scalar(text("SELECT 40 + 2"))
        return {"value": int(value)}

    response = await db_client.get("/_test/session")

    assert response.status_code == 200
    assert response.json() == {"value": 42}


async def test_lifespan_disposes_the_engine_on_shutdown(db_app: FastAPI) -> None:
    engine: AsyncEngine = db_app.state.db_engine

    async with db_app.router.lifespan_context(db_app):
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        assert cast(QueuePool, engine.pool).checkedin() >= 1

    # dispose() closes every pooled connection and swaps in a fresh, empty pool.
    assert cast(QueuePool, engine.pool).checkedin() == 0
