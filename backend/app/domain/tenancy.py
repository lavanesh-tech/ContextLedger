"""Tenant context: *who* is acting, *in which organization*, with *what role*.

Every tenant-scoped service call receives a TenantContext. It is built by
``TenancyService.resolve`` from a verified membership (and, from Phase 13, from
a verified JWT). Services treat ``role`` as a snapshot only: mutating
operations re-read the actor's membership inside their transaction, so a role
revoked a moment ago cannot still be used.
"""

from dataclasses import dataclass
from uuid import UUID

from app.domain.roles import MembershipRole, Permission, has_permission


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: UUID
    user_id: UUID
    role: MembershipRole

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)
