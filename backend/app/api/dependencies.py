"""Shared FastAPI dependencies.

Everything is read from ``app.state`` (set up in ``create_app``) instead of
module-level globals, so tests can build isolated apps with their own settings
and their own database.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_db_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.db_engine)


def get_expected_schema_revision(request: Request) -> str | None:
    return cast(str | None, request.app.state.expected_schema_revision)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, always closed (and rolled back if uncommitted)."""
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.db_sessionmaker)
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_db_engine)]
ExpectedRevisionDep = Annotated[str | None, Depends(get_expected_schema_revision)]
SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
