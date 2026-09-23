"""Hybrid temporal retrieval over REST."""

from fastapi import APIRouter

from app.api.dependencies import ProviderDep, SessionDep, TenantDep
from app.api.errors import problem_responses
from app.schemas.api import SearchRequest
from app.services.retrieval import RetrievalResult, RetrievalService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["search"])


@router.post(
    "/search", summary="Hybrid temporal search", responses=problem_responses(401, 403, 422)
)
async def search(
    body: SearchRequest, ctx: TenantDep, session: SessionDep, provider: ProviderDep
) -> RetrievalResult:
    """Vector + full-text search over facts valid at `valid_at` as known at `known_at`,
    pre-filtered by tenant, privacy scope and metadata; each result explains its score."""
    return await RetrievalService(session, provider).search(ctx, body.to_query())
