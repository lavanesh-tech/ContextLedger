"""The ORM models and the migration history must describe the same schema.

Same check as ``alembic check`` in CI, but runs locally with ``make check`` too.
"""

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401  (registers every model)
from app.db.base import Base
from app.db.migrations import include_name

pytestmark = pytest.mark.integration


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_name": include_name,
        },
    )
    return list(compare_metadata(context, Base.metadata))


async def test_models_match_the_migrated_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        differences = await connection.run_sync(_diff)

    assert differences == []
