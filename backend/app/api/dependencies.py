"""Shared FastAPI dependencies.

Everything is read from ``app.state`` (set up in ``create_app``) instead of
module-level globals, so tests can build isolated apps with their own settings
and their own database.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.errors import AuthenticationRequiredError, ServiceUnavailableError
from app.core.config import Settings
from app.domain.tenancy import TenantContext
from app.provenance.graph import GraphReader
from app.providers.embeddings import EmbeddingProvider
from app.services.tenancy import TenancyService


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


# --- identity and tenancy ------------------------------------------------------------

USER_ID_HEADER = "X-ContextLedger-User-Id"


async def get_principal(request: Request, settings: SettingsDep) -> UUID:
    """The authenticated user's id.

    Phase 12 supports only ``development-headers`` (refused in staging and
    production by settings validation). Phase 13 replaces this with JWT
    verification; every endpoint already depends on this single function.
    """
    if settings.auth_mode != "development-headers":
        raise AuthenticationRequiredError("JWT authentication is not implemented yet (Phase 13)")
    raw = request.headers.get(USER_ID_HEADER)
    if not raw:
        raise AuthenticationRequiredError(f"send your user id in the {USER_ID_HEADER} header")
    try:
        return UUID(raw)
    except ValueError:
        raise AuthenticationRequiredError(f"{USER_ID_HEADER} must be a UUID") from None


PrincipalDep = Annotated[UUID, Depends(get_principal)]


async def get_tenant(
    organization_id: UUID, principal: PrincipalDep, session: SessionDep
) -> TenantContext:
    """TenantContext for the caller inside the organization in the path.

    Unknown organization, non-member and inactive user all fail identically
    (403), so organization ids cannot be probed.
    """
    return await TenancyService(session).resolve(organization_id=organization_id, user_id=principal)


TenantDep = Annotated[TenantContext, Depends(get_tenant)]


def get_embedding_provider(request: Request) -> EmbeddingProvider:
    return cast(EmbeddingProvider, request.app.state.embedding_provider)


def get_graph_reader(request: Request) -> GraphReader:
    reader = cast(GraphReader | None, request.app.state.graph_reader)
    if reader is None:
        raise ServiceUnavailableError("the provenance graph (Neo4j) is not configured")
    return reader


ProviderDep = Annotated[EmbeddingProvider, Depends(get_embedding_provider)]
GraphDep = Annotated[GraphReader, Depends(get_graph_reader)]
