"""Shared FastAPI dependencies.

Everything is read from ``app.state`` (set up in ``create_app``) instead of
module-level globals, so tests can build isolated apps with their own settings
and their own database.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.errors import (
    AuthenticationRequiredError,
    RateLimitedError,
    ServiceUnavailableError,
)
from app.auth.tokens import InvalidTokenError, TokenService
from app.cache.rate_limit import RateLimiter
from app.cache.retrieval import RetrievalCache
from app.core.config import Settings
from app.domain.errors import PermissionDeniedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole, Permission
from app.domain.tenancy import TenantContext
from app.provenance.graph import GraphReader
from app.providers.embeddings import EmbeddingProvider
from app.services.agent_clients import AgentClientService
from app.services.authorization import NOT_A_MEMBER
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


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. Agents are bound to one organization and carry narrowed rights."""

    user_id: UUID
    organization_id: UUID | None = None  # agents: the only organization they may touch
    scopes: frozenset[Permission] | None = None
    max_privacy_scope: PrivacyScope | None = None
    agent_client_id: UUID | None = None


def get_token_service(request: Request) -> TokenService:
    return cast(TokenService, request.app.state.token_service)


TokenServiceDep = Annotated[TokenService, Depends(get_token_service)]


def get_rate_limiter(request: Request) -> RateLimiter:
    return cast(RateLimiter, request.app.state.rate_limiter)


def get_retrieval_cache(request: Request) -> RetrievalCache:
    return cast(RetrievalCache, request.app.state.retrieval_cache)


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
RetrievalCacheDep = Annotated[RetrievalCache, Depends(get_retrieval_cache)]


async def enforce_rate_limit(limiter: RateLimiter, subject: str, limit: int) -> None:
    if limit <= 0:  # 0 disables the limit
        return
    decision = await limiter.hit(subject, limit=limit)
    if not decision.allowed:
        raise RateLimitedError(limit=limit, retry_after_seconds=decision.retry_after_seconds)


async def get_principal(
    request: Request, settings: SettingsDep, tokens: TokenServiceDep, limiter: RateLimiterDep
) -> Principal:
    """Authenticate the caller, then count the request against its rate limit
    (per agent client, or per user; shared by every API process through Redis).

    * ``Authorization: Bearer <jwt>``: always accepted (users and agents).
    * ``X-ContextLedger-User-Id``: only with ``auth_mode=development-headers``
      (refused in staging/production by settings validation).
    """
    principal = _authenticate(request, settings, tokens)
    subject = (
        f"agent:{principal.agent_client_id}"
        if principal.agent_client_id is not None
        else f"user:{principal.user_id}"
    )
    await enforce_rate_limit(limiter, subject, settings.rate_limit_requests_per_minute)
    return principal


def _authenticate(request: Request, settings: Settings, tokens: TokenService) -> Principal:
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationRequiredError("use 'Authorization: Bearer <access token>'")
        try:
            claims = tokens.verify(token.strip())
        except InvalidTokenError:
            raise AuthenticationRequiredError("invalid or expired access token") from None
        return Principal(
            user_id=claims.user_id,
            organization_id=claims.organization_id,
            scopes=claims.scopes,
            max_privacy_scope=claims.max_privacy_scope,
            agent_client_id=claims.agent_client_id,
        )
    if settings.auth_mode != "development-headers":
        raise AuthenticationRequiredError("send 'Authorization: Bearer <access token>'")
    raw = request.headers.get(USER_ID_HEADER)
    if not raw:
        raise AuthenticationRequiredError(
            f"send a Bearer token or (development only) the {USER_ID_HEADER} header"
        )
    try:
        return Principal(user_id=UUID(raw))
    except ValueError:
        raise AuthenticationRequiredError(f"{USER_ID_HEADER} must be a UUID") from None


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_tenant(
    organization_id: UUID, principal: PrincipalDep, session: SessionDep
) -> TenantContext:
    """TenantContext for the caller inside the organization in the path.

    Unknown organization, non-member, inactive user, revoked agent and an agent
    token used for another organization all fail identically (403), so
    organization ids cannot be probed.
    """
    if principal.organization_id is not None and principal.organization_id != organization_id:
        raise PermissionDeniedError(NOT_A_MEMBER)
    if principal.agent_client_id is not None and not await AgentClientService(session).is_active(
        principal.agent_client_id
    ):
        raise PermissionDeniedError(NOT_A_MEMBER)
    ctx = await TenancyService(session).resolve(
        organization_id=organization_id, user_id=principal.user_id
    )
    if principal.agent_client_id is not None and ctx.role is MembershipRole.ADMIN:
        raise PermissionDeniedError("agents may not act with the ADMIN role")
    return replace(
        ctx,
        scopes=principal.scopes,
        max_privacy_scope=principal.max_privacy_scope,
        agent_client_id=principal.agent_client_id,
    )


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
