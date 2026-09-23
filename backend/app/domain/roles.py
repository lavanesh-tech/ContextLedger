"""Roles and permissions (RBAC foundation).

Roles belong to a membership, not to a user: the same person can be ADMIN in
one organization and VIEWER in another. Code checks *permissions*, never role
names, so the role matrix can change without touching every call site.
Enforcement from JWT/OAuth identities arrives in Phase 13.
"""

from enum import StrEnum
from types import MappingProxyType
from typing import Final


class MembershipRole(StrEnum):
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    READ_ORGANIZATION = "organization:read"
    READ_MEMBERS = "members:read"
    MANAGE_MEMBERS = "members:manage"
    READ_FACTS = "facts:read"
    WRITE_FACTS = "facts:write"
    REVOKE_FACTS = "facts:revoke"
    READ_DECISIONS = "decisions:read"
    RECORD_DECISIONS = "decisions:record"
    READ_AUDIT = "audit:read"


_VIEWER: Final = frozenset(
    {
        Permission.READ_ORGANIZATION,
        Permission.READ_MEMBERS,
        Permission.READ_FACTS,
        Permission.READ_DECISIONS,
    }
)
_ENGINEER: Final = _VIEWER | {Permission.WRITE_FACTS, Permission.RECORD_DECISIONS}
_ADMIN: Final = frozenset(Permission)

ROLE_PERMISSIONS: Final = MappingProxyType(
    {
        MembershipRole.ADMIN: _ADMIN,
        MembershipRole.ENGINEER: _ENGINEER,
        MembershipRole.VIEWER: _VIEWER,
    }
)


def has_permission(role: MembershipRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
