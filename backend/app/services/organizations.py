from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError
from app.domain.roles import MembershipRole
from app.domain.validation import normalize_name, normalize_slug
from app.models.organization import Organization
from app.repositories.memberships import MembershipRepository
from app.repositories.organizations import OrganizationRepository
from app.repositories.users import UserRepository


class OrganizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)

    async def create(self, *, name: str, slug: str, creator_user_id: UUID) -> Organization:
        """Create a tenant and make its creator the first ADMIN, atomically.

        An organization without an ADMIN could never be managed, so both rows
        are written in one transaction: either both exist or neither does.
        """
        normalized_slug = normalize_slug(slug)
        normalized_name = normalize_name(name, field="name")

        async with self._session.begin():
            creator = await self._users.get(creator_user_id)
            if creator is None or not creator.is_active:
                raise NotFoundError("creator user not found")
            if await self._organizations.get_by_slug(normalized_slug) is not None:
                raise ConflictError("an organization with this slug already exists")

            organization = Organization(slug=normalized_slug, name=normalized_name)
            self._organizations.add(organization)
            try:
                await self._session.flush()
            except IntegrityError as exc:  # lost a race on the unique slug
                raise ConflictError("an organization with this slug already exists") from exc

            MembershipRepository(self._session, organization.id).new(
                user_id=creator.id, role=MembershipRole.ADMIN
            )
            await self._session.flush()
        return organization
