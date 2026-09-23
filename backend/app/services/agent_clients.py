"""Agent clients: registration by an ADMIN, and the OAuth2 client-credentials exchange."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.secrets import hash_secret, new_client_id, new_client_secret, verify_secret
from app.auth.tokens import TokenService
from app.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.domain.facts import PrivacyScope
from app.domain.roles import ROLE_PERMISSIONS, MembershipRole, Permission
from app.domain.tenancy import TenantContext
from app.domain.validation import normalize_name
from app.models.agent_client import AgentClient
from app.models.user import User
from app.repositories.memberships import MembershipRepository
from app.repositories.organizations import OrganizationRepository
from app.repositories.users import UserRepository
from app.services.authorization import NOT_A_MEMBER, require_permission

AGENT_ROLES = frozenset({MembershipRole.ENGINEER, MembershipRole.VIEWER})


class InvalidClientError(Exception):
    """Unknown client, wrong secret or revoked client (OAuth2 ``invalid_client``)."""


class InvalidScopeError(Exception):
    """Requested a scope the client is not allowed (OAuth2 ``invalid_scope``)."""


@dataclass(frozen=True, slots=True)
class CreatedAgentClient:
    client: AgentClient
    client_secret: str  # shown once; only its hash is stored


@dataclass(frozen=True, slots=True)
class IssuedToken:
    access_token: str
    expires_in: int
    scope: str


def _validate_scopes(role: MembershipRole, scopes: Iterable[Permission]) -> list[str]:
    requested = {Permission(s) for s in scopes}
    if not requested:
        raise ValidationFailedError("an agent needs at least one scope")
    excess = requested - ROLE_PERMISSIONS[role]
    if excess:
        raise ValidationFailedError(f"role {role} cannot grant: {', '.join(sorted(excess))}")
    return sorted(requested)


class AgentClientService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        ctx: TenantContext,
        *,
        name: str,
        role: MembershipRole,
        scopes: Sequence[Permission],
        privacy_ceiling: PrivacyScope = PrivacyScope.INTERNAL,
    ) -> CreatedAgentClient:
        clean_name = normalize_name(name, field="name")
        agent_role = MembershipRole(role)
        if agent_role not in AGENT_ROLES:
            raise ValidationFailedError("agents can be ENGINEER or VIEWER, never ADMIN")
        allowed = _validate_scopes(agent_role, scopes)
        client_id, secret = new_client_id(), new_client_secret()

        async with self._session.begin():
            if not await OrganizationRepository(self._session).lock(ctx.organization_id):
                raise PermissionDeniedError(NOT_A_MEMBER)
            await require_permission(self._session, ctx, Permission.MANAGE_MEMBERS)
            existing = await self._session.scalar(
                select(AgentClient.id).where(
                    AgentClient.organization_id == ctx.organization_id,
                    AgentClient.name == clean_name,
                )
            )
            if existing is not None:
                raise ConflictError("an agent client with this name already exists")
            service_user = User(
                id=uuid4(),
                email=f"{client_id}@agents.contextledger.invalid",
                display_name=f"agent: {clean_name}"[:200],
            )
            UserRepository(self._session).add(service_user)
            await self._session.flush()
            MembershipRepository(self._session, ctx.organization_id).new(
                user_id=service_user.id, role=agent_role
            )
            client = AgentClient(
                organization_id=ctx.organization_id,
                name=clean_name,
                client_id=client_id,
                secret_hash=hash_secret(secret),
                service_user_id=service_user.id,
                allowed_scopes=allowed,
                privacy_ceiling=PrivacyScope(privacy_ceiling),
                created_by_user_id=ctx.user_id,
            )
            self._session.add(client)
            await self._session.flush()
        return CreatedAgentClient(client=client, client_secret=secret)

    async def list_clients(self, ctx: TenantContext) -> Sequence[AgentClient]:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.MANAGE_MEMBERS)
            result = await self._session.scalars(
                select(AgentClient)
                .where(AgentClient.organization_id == ctx.organization_id)
                .order_by(AgentClient.created_at, AgentClient.name)
            )
            return result.all()

    async def revoke(self, ctx: TenantContext, agent_client_id: UUID) -> AgentClient:
        """Revoke a client. Its already-issued tokens stop working on the next request."""
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.MANAGE_MEMBERS)
            client = await self._session.scalar(
                select(AgentClient)
                .where(
                    AgentClient.organization_id == ctx.organization_id,
                    AgentClient.id == agent_client_id,
                )
                .with_for_update()
            )
            if client is None:
                raise NotFoundError("agent client not found")
            if client.revoked_at is None:
                client.revoked_at = datetime.now(UTC)
        return client

    async def exchange(
        self,
        tokens: TokenService,
        *,
        client_id: str,
        client_secret: str,
        requested_scope: str | None,
    ) -> IssuedToken:
        """OAuth2 client credentials: verify the secret, issue a narrowly scoped token."""
        async with self._session.begin():
            client = await self._session.scalar(
                select(AgentClient).where(AgentClient.client_id == client_id)
            )
        # Hash even for unknown clients so response time does not reveal which ids exist.
        stored = client.secret_hash if client is not None else hash_secret("not-a-secret")
        if not verify_secret(client_secret, stored) or client is None:
            raise InvalidClientError("invalid client credentials")
        if client.revoked_at is not None:
            raise InvalidClientError("client has been revoked")

        allowed = {Permission(s) for s in client.allowed_scopes}
        if requested_scope:
            try:
                requested = {Permission(s) for s in requested_scope.split()}
            except ValueError as exc:
                raise InvalidScopeError("unknown scope") from exc
            if not requested <= allowed:
                raise InvalidScopeError("scope exceeds what this client is allowed")
            granted = frozenset(requested)
        else:
            granted = frozenset(allowed)

        token, _ = tokens.issue(
            subject=f"agent:{client.id}",
            user_id=client.service_user_id,
            organization_id=client.organization_id,
            scopes=granted,
            max_privacy_scope=PrivacyScope(client.privacy_ceiling),
        )
        return IssuedToken(
            access_token=token,
            expires_in=int(tokens.ttl.total_seconds()),
            scope=" ".join(sorted(granted)),
        )

    async def is_active(self, agent_client_id: UUID) -> bool:
        async with self._session.begin():
            row = (
                await self._session.execute(
                    select(AgentClient.revoked_at).where(AgentClient.id == agent_client_id)
                )
            ).first()
        return row is not None and row.revoked_at is None
