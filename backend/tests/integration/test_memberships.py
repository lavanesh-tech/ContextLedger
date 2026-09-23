"""Membership rules: RBAC, the last-ADMIN invariant, stale contexts, races."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import (
    ConflictError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
)
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.membership import OrganizationMembership
from app.services.memberships import MembershipService
from tests.integration.factories import add_member, context, make_org, make_user, role_of

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
ADMIN, ENGINEER, VIEWER = MembershipRole.ADMIN, MembershipRole.ENGINEER, MembershipRole.VIEWER


async def _change_role(
    sessions: Sessions, ctx: TenantContext, user_id: UUID, role: MembershipRole
) -> None:
    async with sessions() as session:
        await MembershipService(session).change_role(ctx, user_id=user_id, role=role)


async def _remove(sessions: Sessions, ctx: TenantContext, user_id: UUID) -> None:
    async with sessions() as session:
        await MembershipService(session).remove_member(ctx, user_id=user_id)


async def _admin_count(sessions: Sessions, organization_id: UUID) -> int:
    async with sessions() as session:
        count = await session.scalar(
            select(func.count()).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.role == ADMIN,
            )
        )
    return int(count or 0)


async def test_admin_can_add_members_with_a_role(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    engineer = await make_user(sessions)
    org = await make_org(sessions, admin)

    await add_member(sessions, await context(sessions, org.id, admin.id), engineer, ENGINEER)

    assert await role_of(sessions, org.id, engineer.id) is ENGINEER


@pytest.mark.parametrize("role", [ENGINEER, VIEWER])
async def test_non_admins_cannot_add_members(sessions: Sessions, role: MembershipRole) -> None:
    admin = await make_user(sessions)
    member = await make_user(sessions)
    newcomer = await make_user(sessions)
    org = await make_org(sessions, admin)
    await add_member(sessions, await context(sessions, org.id, admin.id), member, role)

    member_ctx = await context(sessions, org.id, member.id)
    with pytest.raises(PermissionDeniedError):
        await add_member(sessions, member_ctx, newcomer, VIEWER)


async def test_adding_an_existing_member_is_a_conflict(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)
    ctx = await context(sessions, org.id, admin.id)

    with pytest.raises(ConflictError):
        await add_member(sessions, ctx, admin, VIEWER)


async def test_adding_an_inactive_user_is_not_found(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    dormant = await make_user(sessions, active=False)
    org = await make_org(sessions, admin)

    with pytest.raises(NotFoundError):
        await add_member(sessions, await context(sessions, org.id, admin.id), dormant, VIEWER)


async def test_the_last_admin_cannot_be_demoted(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)
    ctx = await context(sessions, org.id, admin.id)

    with pytest.raises(InvariantViolationError):
        await _change_role(sessions, ctx, admin.id, VIEWER)
    assert await role_of(sessions, org.id, admin.id) is ADMIN


async def test_the_last_admin_cannot_be_removed(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    org = await make_org(sessions, admin)
    ctx = await context(sessions, org.id, admin.id)

    with pytest.raises(InvariantViolationError):
        await _remove(sessions, ctx, admin.id)


async def test_an_admin_can_be_demoted_when_another_admin_exists(sessions: Sessions) -> None:
    first = await make_user(sessions)
    second = await make_user(sessions)
    org = await make_org(sessions, first)
    ctx = await context(sessions, org.id, first.id)
    await add_member(sessions, ctx, second, ADMIN)

    await _change_role(sessions, ctx, second.id, ENGINEER)

    assert await role_of(sessions, org.id, second.id) is ENGINEER
    assert await _admin_count(sessions, org.id) == 1


async def test_members_can_be_removed(sessions: Sessions) -> None:
    admin = await make_user(sessions)
    viewer = await make_user(sessions)
    org = await make_org(sessions, admin)
    ctx = await context(sessions, org.id, admin.id)
    await add_member(sessions, ctx, viewer, VIEWER)

    await _remove(sessions, ctx, viewer.id)

    with pytest.raises(PermissionDeniedError):
        await context(sessions, org.id, viewer.id)


async def test_a_stale_context_cannot_use_a_revoked_role(sessions: Sessions) -> None:
    first = await make_user(sessions)
    second = await make_user(sessions)
    target = await make_user(sessions)
    org = await make_org(sessions, first)
    first_ctx = await context(sessions, org.id, first.id)
    await add_member(sessions, first_ctx, second, ADMIN)
    await add_member(sessions, first_ctx, target, VIEWER)
    second_ctx = await context(sessions, org.id, second.id)  # snapshot says ADMIN

    await _change_role(sessions, first_ctx, second.id, VIEWER)  # revoked afterwards

    with pytest.raises(PermissionDeniedError):
        await _change_role(sessions, second_ctx, target.id, ENGINEER)
    assert await role_of(sessions, org.id, target.id) is VIEWER


async def test_concurrent_demotions_always_leave_one_admin(sessions: Sessions) -> None:
    """Two admins demote each other at the same moment, on separate connections.

    Without the organization row lock both transactions could see "2 admins",
    both succeed, and the organization would be left with no ADMIN at all.
    """
    first = await make_user(sessions)
    second = await make_user(sessions)
    org = await make_org(sessions, first)
    first_ctx = await context(sessions, org.id, first.id)
    await add_member(sessions, first_ctx, second, ADMIN)
    second_ctx = await context(sessions, org.id, second.id)

    results = await asyncio.gather(
        _change_role(sessions, first_ctx, second.id, VIEWER),
        _change_role(sessions, second_ctx, first.id, VIEWER),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], PermissionDeniedError | InvariantViolationError)
    assert await _admin_count(sessions, org.id) == 1
