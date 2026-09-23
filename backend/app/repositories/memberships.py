"""Tenant-scoped data access for organization memberships.

A ``MembershipRepository`` is *bound to one organization* when it is created.
None of its methods accept an organization id, so it is impossible to write a
query through it that reads or changes another tenant's rows. Every tenant-owned
repository in later phases follows the same pattern.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.roles import MembershipRole
from app.models.membership import OrganizationMembership


class MembershipRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    def new(self, *, user_id: UUID, role: MembershipRole) -> OrganizationMembership:
        membership = OrganizationMembership(
            organization_id=self.organization_id, user_id=user_id, role=role
        )
        self._session.add(membership)
        return membership

    async def get(self, user_id: UUID) -> OrganizationMembership | None:
        result = await self._session.scalars(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == self.organization_id,
                OrganizationMembership.user_id == user_id,
            )
        )
        return result.one_or_none()

    async def list_members(self) -> Sequence[OrganizationMembership]:
        result = await self._session.scalars(
            select(OrganizationMembership)
            .where(OrganizationMembership.organization_id == self.organization_id)
            .order_by(OrganizationMembership.created_at, OrganizationMembership.id)
        )
        return result.all()

    async def count_with_role(self, role: MembershipRole) -> int:
        count = await self._session.scalar(
            select(func.count()).where(
                OrganizationMembership.organization_id == self.organization_id,
                OrganizationMembership.role == role,
            )
        )
        return int(count or 0)

    async def delete(self, membership: OrganizationMembership) -> None:
        if membership.organization_id != self.organization_id:  # defence in depth
            raise ValueError("membership belongs to a different organization")
        await self._session.delete(membership)
