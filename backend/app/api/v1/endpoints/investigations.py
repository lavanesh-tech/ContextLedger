"""The Historical Decision Investigator over REST."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from app.ai.agent.investigator import MAX_QUESTION_CHARS, DecisionInvestigator, Investigation
from app.ai.agent.tools import ServiceBackend
from app.api.dependencies import (
    GeneratorDep,
    ProviderDep,
    RetrievalCacheDep,
    SessionDep,
    SettingsDep,
    TenantDep,
)
from app.api.errors import problem_responses
from app.schemas.api import Request
from app.services.decisions import DecisionService
from app.services.investigations import InvestigationService
from app.services.retrieval import RetrievalService
from app.services.temporal import TemporalService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["investigations"])


class InvestigationRequest(Request):
    question: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        examples=["Why was decision 7f1c... approved, and has a fact it relied on changed?"],
    )


@router.post(
    "/investigations",
    summary="Investigate a past decision with a bounded, read-only agent",
    responses=problem_responses(401, 403, 422, 429, 503),
)
async def investigate(
    body: InvestigationRequest,
    ctx: TenantDep,
    session: SessionDep,
    embeddings: ProviderDep,
    cache: RetrievalCacheDep,
    generator: GeneratorDep,
    settings: SettingsDep,
) -> Investigation:
    """Runs the Historical Decision Investigator: a tool-calling agent with read-only
    tools (decision receipt, fact lineage, decisions relying on a fact, fact search),
    all restricted to your organization, permissions and privacy ceiling. Needs
    `facts:read` and `decisions:read`. Citations are checked against ids the tools
    returned; the run and every tool call are stored as an immutable trace."""
    backend = ServiceBackend(
        decisions=DecisionService(session, embeddings),
        temporal=TemporalService(session),
        retrieval=RetrievalService(session, embeddings, cache=cache),
    )
    investigator = DecisionInvestigator(
        backend,
        generator,
        max_steps=settings.llm_agent_max_steps,
        max_tool_calls=settings.llm_agent_max_tool_calls,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    return await InvestigationService(session, investigator).investigate(ctx, body.question)


@router.get(
    "/investigations/{run_id}",
    summary="The stored trace of one of your investigations",
    responses=problem_responses(401, 403, 404),
)
async def get_investigation(run_id: UUID, ctx: TenantDep, session: SessionDep) -> Investigation:
    """Only the user who requested the investigation can read it back."""
    return await InvestigationService(session).get(ctx, run_id)
