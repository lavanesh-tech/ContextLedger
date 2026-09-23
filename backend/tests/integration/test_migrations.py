"""Migrations applied to a real PostgreSQL."""

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.support import (
    alembic_config,
    current_revision,
    extension_installed,
    run_alembic,
)

pytestmark = pytest.mark.integration


def _head() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def test_database_is_migrated_to_head(engine: AsyncEngine) -> None:
    assert await current_revision(engine) == _head()


async def test_pgvector_extension_is_installed_by_migrations(engine: AsyncEngine) -> None:
    assert await extension_installed(engine, "vector")


async def test_full_downgrade_and_upgrade_round_trip(engine: AsyncEngine) -> None:
    await run_alembic(engine, "downgrade", "base")
    assert await current_revision(engine) is None
    assert not await extension_installed(engine, "vector")

    await run_alembic(engine, "upgrade", "head")
    assert await current_revision(engine) == _head()
    assert await extension_installed(engine, "vector")
