from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import PermissionDeniedError
from app.domain.tenancy import TenantContext
from app.repositories.memberships import MembershipRepository
from app.repositories.users import UserRepository
from app.services.authorization import NOT_A_MEMBER

__all__ = ["NOT_A_MEMBER", "TenancyService"]


class TenancyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def resolve(self, *, organization_id: UUID, user_id: UUID) -> TenantContext:
        """Build the TenantContext for ``user_id`` acting inside ``organization_id``."""
        async with self._session.begin():
            user = await self._users.get(user_id)
            membership = await MembershipRepository(self._session, organization_id).get(user_id)
            if user is None or not user.is_active or membership is None:
                raise PermissionDeniedError(NOT_A_MEMBER)
            return TenantContext(
                organization_id=organization_id, user_id=user_id, role=membership.role
            )
