"""Helpers for tests that need a real PostgreSQL database."""

import os
import re
from pathlib import Path
from typing import Literal, NoReturn

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Environment, Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

TEST_DATABASE_URL_ENV = "CONTEXTLEDGER_TEST_DATABASE_URL"
REQUIRE_DB_ENV = "CONTEXTLEDGER_REQUIRE_DB_TESTS"

# The test database is dropped and recreated on every run, so refuse anything
# that is not obviously a throwaway test database.
_SAFE_TEST_DB_NAME = re.compile(r"^[a-z][a-z0-9_]{0,55}_test$")


def unavailable(reason: str) -> NoReturn:
    """Skip locally, but fail in CI (where the database must exist)."""
    if os.environ.get(REQUIRE_DB_ENV) == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def get_test_database_url() -> URL:
    raw = os.environ.get(TEST_DATABASE_URL_ENV)
    if not raw:
        unavailable(f"{TEST_DATABASE_URL_ENV} is not set (use `make test` with `make up` running)")
    url = make_url(raw)
    if not url.database or not _SAFE_TEST_DB_NAME.fullmatch(url.database):
        pytest.fail(
            f"refusing to use database {url.database!r}: test database names must end in '_test'",
            pytrace=False,
        )
    return url


async def recreate_database(url: URL) -> None:
    """Drop and create the test database so every run starts from an empty schema."""
    admin = create_async_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"timeout": 5},
    )
    name = url.database  # validated by _SAFE_TEST_DB_NAME: safe to quote as an identifier
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


def alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logging"] = False
    return config


Action = Literal["upgrade", "downgrade"]


def _run_alembic(connection: Connection, action: Action, target: str) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    if action == "upgrade":
        command.upgrade(config, target)
    else:
        command.downgrade(config, target)


async def run_alembic(engine: AsyncEngine, action: Action, target: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_run_alembic, action, target)


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        if not await connection.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL")):
            return None
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        revision = result.scalars().first()
        return None if revision is None else str(revision)


async def extension_installed(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = :name"), {"name": name}
        )
    return bool(found)


def settings_for(url: URL) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        db_host=url.host or "localhost",
        db_port=url.port or 5432,
        db_user=url.username or "postgres",
        db_password=SecretStr(url.password or ""),
        db_name=url.database or "",
        alembic_ini_path=ALEMBIC_INI,
    )


NEO4J_URI_ENV = "CONTEXTLEDGER_TEST_NEO4J_URI"
NEO4J_USER_ENV = "CONTEXTLEDGER_TEST_NEO4J_USER"
NEO4J_PASSWORD_ENV = "CONTEXTLEDGER_TEST_NEO4J_PASSWORD"


def get_test_neo4j() -> tuple[str, str, str]:
    uri = os.environ.get(NEO4J_URI_ENV)
    if not uri:
        unavailable(f"{NEO4J_URI_ENV} is not set (use `make test` with `make up` running)")
    return uri, os.environ.get(NEO4J_USER_ENV, "neo4j"), os.environ.get(NEO4J_PASSWORD_ENV, "")
