from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization


class OrganizationRepository:
    """Data access for organizations (the tenants themselves). Never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, organization: Organization) -> None:
        self._session.add(organization)

    async def get(self, organization_id: UUID) -> Organization | None:
        return await self._session.get(Organization, organization_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self._session.scalars(select(Organization).where(Organization.slug == slug))
        return result.one_or_none()

    async def lock(self, organization_id: UUID) -> bool:
        """Take a row lock on the organization until the transaction ends.

        Serialises membership changes per organization, so two concurrent
        requests cannot both remove "the other" ADMIN and leave none.
        Returns False if the organization does not exist.
        """
        locked_id = await self._session.scalar(
            select(Organization.id).where(Organization.id == organization_id).with_for_update()
        )
        return locked_id is not None
