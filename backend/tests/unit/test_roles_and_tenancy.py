import uuid

import pytest

from app.domain.roles import ROLE_PERMISSIONS, MembershipRole, Permission, has_permission
from app.domain.tenancy import TenantContext


def test_every_role_has_a_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(MembershipRole)


def test_admin_has_every_permission() -> None:
    assert all(has_permission(MembershipRole.ADMIN, p) for p in Permission)


@pytest.mark.parametrize(
    "permission",
    [
        Permission.WRITE_FACTS,
        Permission.RECORD_DECISIONS,
        Permission.MANAGE_MEMBERS,
        Permission.REVOKE_FACTS,
        Permission.READ_AUDIT,
    ],
)
def test_viewer_cannot_write_or_administer(permission: Permission) -> None:
    assert not has_permission(MembershipRole.VIEWER, permission)


def test_engineer_can_write_facts_but_not_manage_members_or_revoke() -> None:
    assert has_permission(MembershipRole.ENGINEER, Permission.WRITE_FACTS)
    assert has_permission(MembershipRole.ENGINEER, Permission.RECORD_DECISIONS)
    assert not has_permission(MembershipRole.ENGINEER, Permission.MANAGE_MEMBERS)
    assert not has_permission(MembershipRole.ENGINEER, Permission.REVOKE_FACTS)


def test_roles_are_strictly_ordered_by_privilege() -> None:
    viewer = ROLE_PERMISSIONS[MembershipRole.VIEWER]
    engineer = ROLE_PERMISSIONS[MembershipRole.ENGINEER]
    admin = ROLE_PERMISSIONS[MembershipRole.ADMIN]

    assert viewer < engineer < admin


def test_permission_matrix_cannot_be_mutated_at_runtime() -> None:
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[MembershipRole.VIEWER] = frozenset(Permission)  # type: ignore[index]


def test_tenant_context_checks_permissions_by_role() -> None:
    ctx = TenantContext(
        organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=MembershipRole.VIEWER
    )

    assert ctx.can(Permission.READ_FACTS)
    assert not ctx.can(Permission.WRITE_FACTS)
