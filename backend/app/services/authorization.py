"""Authorization shared by every tenant-scoped service.

Always checks the actor's *current* membership in the database inside the
caller's transaction, never only the TenantContext snapshot.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import PermissionDeniedError
from app.domain.roles import MembershipRole, Permission, has_permission
from app.domain.tenancy import TenantContext
from app.repositories.memberships import MembershipRepository
from app.repositories.users import UserRepository

# One message for "organization does not exist", "not a member" and "user
# inactive", so callers cannot probe which organization ids exist.
NOT_A_MEMBER = "not a member of this organization"


async def require_permission(
    session: AsyncSession, ctx: TenantContext, permission: Permission
) -> MembershipRole:
    user = await UserRepository(session).get(ctx.user_id)
    membership = await MembershipRepository(session, ctx.organization_id).get(ctx.user_id)
    if user is None or not user.is_active or membership is None:
        raise PermissionDeniedError(NOT_A_MEMBER)
    if not has_permission(membership.role, permission):
        raise PermissionDeniedError(f"role {membership.role} lacks permission {permission}")
    return membership.role
