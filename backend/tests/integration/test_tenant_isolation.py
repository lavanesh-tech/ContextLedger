"""Cross-tenant attacks: an actor in organization A must never see or change B."""

from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import NotFoundError, PermissionDeniedError
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.user import User
from app.repositories.memberships import MembershipRepository
from app.services.memberships import MembershipService
from app.services.tenancy import NOT_A_MEMBER
from tests.integration.factories import add_member, context, make_org, make_user, role_of

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


async def test_cannot_resolve_a_context_in_someone_elses_organization(sessions: Sessions) -> None:
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    bobs_org = await make_org(sessions, bob)

    with pytest.raises(PermissionDeniedError, match=NOT_A_MEMBER):
        await context(sessions, bobs_org.id, alice.id)


async def test_unknown_organization_looks_the_same_as_not_a_member(sessions: Sessions) -> None:
    alice = await make_user(sessions)

    with pytest.raises(PermissionDeniedError, match=NOT_A_MEMBER):
        await context(sessions, uuid4(), alice.id)


async def test_inactive_users_cannot_act_in_their_organization(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    viewer = await make_user(sessions)
    org = await make_org(sessions, admin)
    await add_member(
        sessions, await context(sessions, org.id, admin.id), viewer, MembershipRole.VIEWER
    )

    async with sessions() as session, session.begin():
        await session.execute(update(User).where(User.id == viewer.id).values(is_active=False))

    with pytest.raises(PermissionDeniedError):
        await context(sessions, org.id, viewer.id)


async def test_member_listing_only_contains_the_callers_organization(sessions: Sessions) -> None:
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    alices_org = await make_org(sessions, alice)
    await make_org(sessions, bob)

    ctx = await context(sessions, alices_org.id, alice.id)
    async with sessions() as session:
        members = await MembershipService(session).list_members(ctx)

    assert {m.user_id for m in members} == {alice.id}
    assert {m.organization_id for m in members} == {alices_org.id}


async def test_admin_of_a_cannot_change_roles_in_b(sessions: Sessions) -> None:
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    bobs_engineer = await make_user(sessions)
    alices_org = await make_org(sessions, alice)
    bobs_org = await make_org(sessions, bob)
    await add_member(
        sessions,
        await context(sessions, bobs_org.id, bob.id),
        bobs_engineer,
        MembershipRole.ENGINEER,
    )

    alice_ctx = await context(sessions, alices_org.id, alice.id)
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await MembershipService(session).change_role(
                alice_ctx, user_id=bobs_engineer.id, role=MembershipRole.VIEWER
            )

    assert await role_of(sessions, bobs_org.id, bobs_engineer.id) is MembershipRole.ENGINEER


async def test_admin_of_a_cannot_remove_members_of_b(sessions: Sessions) -> None:
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    alices_org = await make_org(sessions, alice)
    bobs_org = await make_org(sessions, bob)

    alice_ctx = await context(sessions, alices_org.id, alice.id)
    async with sessions() as session:
        with pytest.raises(NotFoundError):
            await MembershipService(session).remove_member(alice_ctx, user_id=bob.id)

    assert await role_of(sessions, bobs_org.id, bob.id) is MembershipRole.ADMIN


async def test_forged_context_for_another_organization_is_rejected(sessions: Sessions) -> None:
    """A context built by hand (as a bug or a tampered token might) is re-verified."""
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    await make_org(sessions, alice)
    bobs_org = await make_org(sessions, bob)

    forged = TenantContext(organization_id=bobs_org.id, user_id=alice.id, role=MembershipRole.ADMIN)
    async with sessions() as session:
        with pytest.raises(PermissionDeniedError):
            await MembershipService(session).list_members(forged)


async def test_tenant_bound_repository_cannot_see_other_tenants(sessions: Sessions) -> None:
    alice = await make_user(sessions)
    bob = await make_user(sessions)
    alices_org = await make_org(sessions, alice)
    await make_org(sessions, bob)

    async with sessions() as session:
        repository = MembershipRepository(session, alices_org.id)
        assert await repository.get(bob.id) is None
        assert [m.user_id for m in await repository.list_members()] == [alice.id]
