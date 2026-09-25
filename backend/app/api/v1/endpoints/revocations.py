"""Fact revocation and decision impact."""

from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import Field

from app.api.dependencies import RetrievalCacheDep, SessionDep, TenantDep
from app.api.errors import problem_responses
from app.schemas.api import Request
from app.services.revocations import MAX_LIST, MAX_REASON_CHARS, RevocationReport, RevocationService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["revocations"])


class RevokeRequest(Request):
    reason: str = Field(
        min_length=1,
        max_length=MAX_REASON_CHARS,
        examples=["Billing import used the wrong currency for this limit."],
    )
    version_id: UUID | None = Field(
        default=None, description="The version to revoke (default: the fact's latest version)."
    )


@router.post(
    "/facts/{fact_id}/revoke",
    status_code=status.HTTP_201_CREATED,
    summary="Revoke a fact version and see which decisions depended on it",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def revoke(
    fact_id: UUID,
    body: RevokeRequest,
    ctx: TenantDep,
    session: SessionDep,
    cache: RetrievalCacheDep,
) -> RevocationReport:
    """Marks a version as wrong (not merely superseded). It stops appearing in current
    and "as known now" answers, stays in the history and in decision receipts (marked
    `revoked_at`), and every decision whose frozen context held it is listed, with
    `relied_on` when the decision cited it. Needs `facts:write` and `decisions:read`."""
    report = await RevocationService(session).revoke(
        ctx, fact_id, reason=body.reason, version_id=body.version_id
    )
    await cache.invalidate(ctx.organization_id)
    return report


@router.get(
    "/facts/{fact_id}/impact",
    summary="Revoked versions of a fact and the decisions they affect",
    responses=problem_responses(401, 403, 404),
)
async def fact_impact(fact_id: UUID, ctx: TenantDep, session: SessionDep) -> list[RevocationReport]:
    return await RevocationService(session).impact_of_fact(ctx, fact_id)


@router.get(
    "/fact-versions/{version_id}/revocation",
    summary="The revocation of one fact version and its impact",
    responses=problem_responses(401, 403, 404),
)
async def version_revocation(
    version_id: UUID, ctx: TenantDep, session: SessionDep
) -> RevocationReport:
    return await RevocationService(session).impact_of_version(ctx, version_id)


@router.get(
    "/revocations",
    summary="Recent revocations",
    responses=problem_responses(401, 403, 422),
)
async def list_revocations(
    ctx: TenantDep, session: SessionDep, limit: int = Query(default=20, ge=1, le=MAX_LIST)
) -> list[RevocationReport]:
    return await RevocationService(session).list_revocations(ctx, limit=limit)
