from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.roles import MembershipRole
from app.services.memberships import MembershipService
from app.services.organizations import OrganizationService
from app.services.users import UserService
from tests.integration.factories import context, make_org, make_user, unique

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


async def test_register_normalises_email(sessions: Sessions) -> None:
    local = unique("Alice")
    async with sessions() as session:
        user = await UserService(session).register(
            email=f"  {local}@Example.COM ", display_name="  Alice   Doe "
        )

    assert user.email == f"{local.lower()}@example.com"
    assert user.display_name == "Alice Doe"
    assert user.is_active is True
    assert user.created_at.tzinfo is not None


async def test_email_uniqueness_ignores_case(sessions: Sessions) -> None:
    local = unique("bob")
    async with sessions() as session:
        await UserService(session).register(email=f"{local}@example.com", display_name="Bob")

    async with sessions() as session:
        with pytest.raises(ConflictError):
            await UserService(session).register(
                email=f"{local.upper()}@EXAMPLE.com", display_name="B"
            )


async def test_creating_an_organization_makes_the_creator_its_admin(sessions: Sessions) -> None:
    creator = await make_user(sessions)
    slug = unique("acme")

    async with sessions() as session:
        org = await OrganizationService(session).create(
            name=" Acme  Corp ", slug=f" {slug.upper()} ", creator_user_id=creator.id
        )

    assert org.slug == slug
    assert org.name == "Acme Corp"
    ctx = await context(sessions, org.id, creator.id)
    assert ctx.role is MembershipRole.ADMIN
    async with sessions() as session:
        members = await MembershipService(session).list_members(ctx)
    assert [m.user_id for m in members] == [creator.id]


async def test_duplicate_slug_is_a_conflict(sessions: Sessions) -> None:
    creator = await make_user(sessions)
    org = await make_org(sessions, creator)

    async with sessions() as session:
        with pytest.raises(ConflictError):
            await OrganizationService(session).create(
                name="Other", slug=org.slug, creator_user_id=creator.id
            )


async def test_invalid_slug_is_rejected_before_touching_the_database(sessions: Sessions) -> None:
    creator = await make_user(sessions)

    async with sessions() as session:
        with pytest.raises(ValidationFailedError):
            await OrganizationService(session).create(
                name="Bad", slug="Not A Slug!", creator_user_id=creator.id
            )


async def test_unknown_creator_is_not_found(sessions: Sessions) -> None:
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await OrganizationService(session).create(
                name="Ghost", slug=unique("ghost"), creator_user_id=uuid4()
            )


async def test_inactive_creator_is_not_found(sessions: Sessions) -> None:
    inactive = await make_user(sessions, active=False)

    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await OrganizationService(session).create(
                name="Dormant", slug=unique("dormant"), creator_user_id=inactive.id
            )
