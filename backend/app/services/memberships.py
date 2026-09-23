"""Membership management inside one organization.

Rules:
* Only members with ``MANAGE_MEMBERS`` (ADMIN) can add, re-role or remove members.
* An organization must always keep at least one ADMIN.
* Mutations lock the organization row first, then re-read the *actor's*
  membership. Two concurrent requests therefore run one after the other, and
  a role that was just revoked cannot still be used from a stale context.
* Users outside the organization are reported as "not found", never leaked.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import (
    ConflictError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
)
from app.domain.roles import MembershipRole, Permission, has_permission
from app.domain.tenancy import TenantContext
from app.models.membership import OrganizationMembership
from app.repositories.memberships import MembershipRepository
from app.repositories.organizations import OrganizationRepository
from app.repositories.users import UserRepository

LAST_ADMIN = "an organization must keep at least one ADMIN"


class MembershipService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)

    async def list_members(self, ctx: TenantContext) -> Sequence[OrganizationMembership]:
        async with self._session.begin():
            members = self._members(ctx)
            await self._authorize(ctx, members, Permission.READ_MEMBERS)
            return await members.list_members()

    async def add_member(
        self, ctx: TenantContext, *, user_id: UUID, role: MembershipRole
    ) -> OrganizationMembership:
        async with self._session.begin():
            members = self._members(ctx)
            await self._authorize(ctx, members, Permission.MANAGE_MEMBERS, lock=True)

            user = await self._users.get(user_id)
            if user is None or not user.is_active:
                raise NotFoundError("user not found")
            if await members.get(user_id) is not None:
                raise ConflictError("user is already a member of this organization")

            membership = members.new(user_id=user_id, role=role)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                raise ConflictError("user is already a member of this organization") from exc
        return membership

    async def change_role(
        self, ctx: TenantContext, *, user_id: UUID, role: MembershipRole
    ) -> OrganizationMembership:
        async with self._session.begin():
            members = self._members(ctx)
            await self._authorize(ctx, members, Permission.MANAGE_MEMBERS, lock=True)

            membership = await self._require_member(members, user_id)
            if membership.role is MembershipRole.ADMIN and role is not MembershipRole.ADMIN:
                await self._ensure_another_admin(members)
            membership.role = role
            await self._session.flush()
        return membership

    async def remove_member(self, ctx: TenantContext, *, user_id: UUID) -> None:
        async with self._session.begin():
            members = self._members(ctx)
            await self._authorize(ctx, members, Permission.MANAGE_MEMBERS, lock=True)

            membership = await self._require_member(members, user_id)
            if membership.role is MembershipRole.ADMIN:
                await self._ensure_another_admin(members)
            await members.delete(membership)

    # --- helpers ---------------------------------------------------------------

    def _members(self, ctx: TenantContext) -> MembershipRepository:
        return MembershipRepository(self._session, ctx.organization_id)

    async def _authorize(
        self,
        ctx: TenantContext,
        members: MembershipRepository,
        permission: Permission,
        *,
        lock: bool = False,
    ) -> None:
        """Authorize against the actor's *current* membership, not the context snapshot."""
        if lock and not await self._organizations.lock(ctx.organization_id):
            raise PermissionDeniedError("not a member of this organization")
        actor = await self._users.get(ctx.user_id)
        current = await members.get(ctx.user_id)
        if actor is None or not actor.is_active or current is None:
            raise PermissionDeniedError("not a member of this organization")
        if not has_permission(current.role, permission):
            raise PermissionDeniedError(f"role {current.role} lacks permission {permission}")

    @staticmethod
    async def _require_member(
        members: MembershipRepository, user_id: UUID
    ) -> OrganizationMembership:
        membership = await members.get(user_id)
        if membership is None:
            raise NotFoundError("membership not found")
        return membership

    @staticmethod
    async def _ensure_another_admin(members: MembershipRepository) -> None:
        if await members.count_with_role(MembershipRole.ADMIN) <= 1:
            raise InvariantViolationError(LAST_ADMIN)
