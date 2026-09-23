"""Alembic environment: runs migrations with the async (asyncpg) driver.

URL resolution order:
1. ``config.attributes["connection"]``: an existing sync Connection handed in
   by tests (Alembic "connection sharing" pattern for asyncio).
2. ``config.attributes["database_url"]``: an explicit URL (tests).
3. Application settings (``CONTEXTLEDGER_DB_*``): normal CLI use.
"""

import asyncio
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> URL:
    override = config.attributes.get("database_url")
    if override is not None:
        return make_url(override)
    return get_settings().database_url


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    _configure(
        url=_database_url().render_as_string(hide_password=True),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_with_connection)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        _run_with_connection(shared_connection)
    else:
        asyncio.run(_run_async())


if config.attributes.get("configure_logging", True):
    configure_logging(get_settings())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
