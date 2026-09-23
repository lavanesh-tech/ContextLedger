"""Organizations (tenants) and their members."""

from uuid import UUID

from fastapi import APIRouter, Response, status

from app.api.dependencies import PrincipalDep, SessionDep, TenantDep
from app.api.errors import problem_responses
from app.domain.errors import PermissionDeniedError
from app.schemas.api import (
    MemberAdd,
    MemberOut,
    MemberRoleChange,
    OrganizationCreate,
    OrganizationOut,
)
from app.services.memberships import MembershipService
from app.services.organizations import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization (you become its first ADMIN)",
    responses=problem_responses(401, 409, 422),
)
async def create_organization(
    body: OrganizationCreate, principal: PrincipalDep, session: SessionDep
) -> OrganizationOut:
    if principal.agent_client_id is not None:
        raise PermissionDeniedError("agents cannot create organizations")
    organization = await OrganizationService(session).create(
        name=body.name, slug=body.slug, creator_user_id=principal.user_id
    )
    return OrganizationOut.model_validate(organization)


@router.get(
    "/{organization_id}/members",
    summary="List members",
    responses=problem_responses(401, 403),
)
async def list_members(ctx: TenantDep, session: SessionDep) -> list[MemberOut]:
    return [MemberOut.model_validate(m) for m in await MembershipService(session).list_members(ctx)]


@router.post(
    "/{organization_id}/members",
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def add_member(body: MemberAdd, ctx: TenantDep, session: SessionDep) -> MemberOut:
    member = await MembershipService(session).add_member(ctx, user_id=body.user_id, role=body.role)
    return MemberOut.model_validate(member)


@router.patch(
    "/{organization_id}/members/{user_id}",
    summary="Change a member's role",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def change_role(
    user_id: UUID, body: MemberRoleChange, ctx: TenantDep, session: SessionDep
) -> MemberOut:
    member = await MembershipService(session).change_role(ctx, user_id=user_id, role=body.role)
    return MemberOut.model_validate(member)


@router.delete(
    "/{organization_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
    responses=problem_responses(401, 403, 404, 409),
)
async def remove_member(user_id: UUID, ctx: TenantDep, session: SessionDep) -> Response:
    await MembershipService(session).remove_member(ctx, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
