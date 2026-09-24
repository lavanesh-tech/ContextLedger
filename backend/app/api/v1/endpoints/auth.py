"""Access tokens (OAuth2 client credentials for agents) and agent client management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, status
from fastapi.responses import JSONResponse

from app.api.dependencies import (
    RateLimiterDep,
    SessionDep,
    SettingsDep,
    TenantDep,
    TokenServiceDep,
)
from app.api.errors import problem_responses
from app.core.config import Environment
from app.domain.errors import NotFoundError
from app.repositories.users import UserRepository
from app.schemas.api import (
    AgentClientCreate,
    AgentClientCreated,
    AgentClientOut,
    DevTokenRequest,
    TokenResponse,
)
from app.services.agent_clients import AgentClientService, InvalidClientError, InvalidScopeError

router = APIRouter(tags=["auth"])


def _oauth_error(
    status_code: int, error: str, description: str, *, retry_after: int | None = None
) -> JSONResponse:
    """RFC 6749 section 5.2 error body (OAuth clients expect this format, not problem+json)."""
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if status_code == 401:
        headers["WWW-Authenticate"] = 'Basic realm="contextledger"'
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=headers,
    )


@router.post(
    "/oauth/token",
    summary="OAuth2 token endpoint (client credentials)",
    response_model=TokenResponse,
    responses={
        400: {"description": "invalid_request / invalid_scope"},
        401: {"description": "invalid_client"},
        429: {"description": "too many token requests for this client_id"},
    },
)
async def token(
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str, Form()],
    session: SessionDep,
    tokens: TokenServiceDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    scope: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Exchange an agent's client credentials for a short-lived, scoped access token.

    Limited per client_id (whether or not it exists), which caps secret guessing."""
    limit = settings.rate_limit_token_requests_per_minute
    if limit > 0:
        decision = await limiter.hit(f"oauth-client:{client_id[:128]}", limit=limit)
        if not decision.allowed:
            return _oauth_error(
                429,
                "too_many_requests",
                "too many token requests; retry later",
                retry_after=decision.retry_after_seconds,
            )
    if grant_type != "client_credentials":
        return _oauth_error(400, "unsupported_grant_type", "only client_credentials is supported")
    try:
        issued = await AgentClientService(session).exchange(
            tokens, client_id=client_id, client_secret=client_secret, requested_scope=scope
        )
    except InvalidClientError:
        return _oauth_error(401, "invalid_client", "client authentication failed")
    except InvalidScopeError as exc:
        return _oauth_error(400, "invalid_scope", str(exc))
    body = TokenResponse(
        access_token=issued.access_token, expires_in=issued.expires_in, scope=issued.scope
    )
    return JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})


@router.post(
    "/auth/dev-token",
    summary="Issue a user token (local and test environments only)",
    responses=problem_responses(404),
)
async def dev_token(
    body: DevTokenRequest, settings: SettingsDep, session: SessionDep, tokens: TokenServiceDep
) -> TokenResponse:
    """Human sign-in via an external identity provider is out of scope for this project;
    this endpoint lets developers obtain a user token locally. It does not exist
    (404) in staging or production."""
    if settings.environment not in {Environment.LOCAL, Environment.TEST}:
        raise NotFoundError("not found")
    async with session.begin():
        user = await UserRepository(session).get(body.user_id)
    if user is None or not user.is_active:
        raise NotFoundError("user not found")
    token, _ = tokens.issue(subject=f"user:{user.id}", user_id=user.id)
    return TokenResponse(access_token=token, expires_in=int(tokens.ttl.total_seconds()))


@router.post(
    "/organizations/{organization_id}/agent-clients",
    status_code=status.HTTP_201_CREATED,
    summary="Register an agent (returns its secret once)",
    responses=problem_responses(401, 403, 409, 422),
)
async def create_agent_client(
    body: AgentClientCreate, ctx: TenantDep, session: SessionDep
) -> AgentClientCreated:
    created = await AgentClientService(session).create(ctx, **body.model_dump())
    return AgentClientCreated(
        **AgentClientOut.model_validate(created.client).model_dump(),
        client_secret=created.client_secret,
    )


@router.get(
    "/organizations/{organization_id}/agent-clients",
    summary="List agents",
    responses=problem_responses(401, 403),
)
async def list_agent_clients(ctx: TenantDep, session: SessionDep) -> list[AgentClientOut]:
    clients = await AgentClientService(session).list_clients(ctx)
    return [AgentClientOut.model_validate(c) for c in clients]


@router.delete(
    "/organizations/{organization_id}/agent-clients/{agent_client_id}",
    summary="Revoke an agent (its tokens stop working immediately)",
    responses=problem_responses(401, 403, 404),
)
async def revoke_agent_client(
    agent_client_id: UUID, ctx: TenantDep, session: SessionDep
) -> AgentClientOut:
    return AgentClientOut.model_validate(
        await AgentClientService(session).revoke(ctx, agent_client_id)
    )
