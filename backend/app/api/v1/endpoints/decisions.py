"""Context snapshots, decisions and receipts."""

from uuid import UUID

from fastapi import APIRouter, status

from app.api.dependencies import ProviderDep, SessionDep, TenantDep
from app.api.errors import problem_responses
from app.schemas.api import DecisionCreate, DecisionRefs, SearchRequest
from app.services.decisions import CapturedContext, DecisionReceipt, DecisionService, RecordDecision

router = APIRouter(prefix="/organizations/{organization_id}", tags=["decisions"])


@router.post(
    "/context-snapshots",
    status_code=status.HTTP_201_CREATED,
    summary="Retrieve and freeze decision context",
    responses=problem_responses(401, 403, 422),
)
async def capture_context(
    body: SearchRequest, ctx: TenantDep, session: SessionDep, provider: ProviderDep
) -> CapturedContext:
    """Runs hybrid search with `known_at` pinned (default: now) and stores the result
    immutably. Cite its fact versions when recording the decision."""
    return await DecisionService(session, provider).capture_context(ctx, body.to_query())


@router.post(
    "/decisions",
    status_code=status.HTTP_201_CREATED,
    summary="Record a decision and get its sealed receipt",
    responses=problem_responses(401, 403, 404, 422),
)
async def record_decision(
    body: DecisionCreate, ctx: TenantDep, session: SessionDep, provider: ProviderDep
) -> DecisionReceipt:
    return await DecisionService(session, provider).record_decision(
        ctx, RecordDecision(**body.model_dump())
    )


@router.get(
    "/decisions/{decision_id}/receipt",
    summary="A decision receipt, re-verified on read",
    responses=problem_responses(401, 403, 404),
)
async def get_receipt(
    decision_id: UUID, ctx: TenantDep, session: SessionDep, provider: ProviderDep
) -> DecisionReceipt:
    return await DecisionService(session, provider).receipt(ctx, decision_id)


@router.get(
    "/fact-versions/{version_id}/decisions",
    summary="Decisions that relied on a fact version",
    responses=problem_responses(401, 403),
)
async def decisions_relying_on(
    version_id: UUID, ctx: TenantDep, session: SessionDep, provider: ProviderDep
) -> DecisionRefs:
    ids = await DecisionService(session, provider).decisions_relying_on(ctx, version_id)
    return DecisionRefs(decision_ids=ids)
