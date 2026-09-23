"""Test-data builders that go through the real services (so rules are exercised)."""

from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.organization import Organization
from app.models.user import User
from app.services.memberships import MembershipService
from app.services.organizations import OrganizationService
from app.services.tenancy import TenancyService
from app.services.users import UserService

Sessions = async_sessionmaker[AsyncSession]


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def make_user(sessions: Sessions, *, active: bool = True) -> User:
    async with sessions() as session:
        user = await UserService(session).register(
            email=f"{unique('user')}@example.com", display_name="Test User"
        )
    if not active:
        async with sessions() as session, session.begin():
            await session.execute(update(User).where(User.id == user.id).values(is_active=False))
    return user


async def make_org(sessions: Sessions, creator: User) -> Organization:
    async with sessions() as session:
        return await OrganizationService(session).create(
            name="Test Org", slug=unique("org"), creator_user_id=creator.id
        )


async def context(sessions: Sessions, organization_id: UUID, user_id: UUID) -> TenantContext:
    async with sessions() as session:
        return await TenancyService(session).resolve(
            organization_id=organization_id, user_id=user_id
        )


async def add_member(
    sessions: Sessions, actor: TenantContext, user: User, role: MembershipRole
) -> None:
    async with sessions() as session:
        await MembershipService(session).add_member(actor, user_id=user.id, role=role)


async def role_of(sessions: Sessions, organization_id: UUID, user_id: UUID) -> MembershipRole:
    return (await context(sessions, organization_id, user_id)).role
