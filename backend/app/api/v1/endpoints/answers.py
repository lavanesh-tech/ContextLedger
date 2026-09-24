"""Grounded answers over REST: POST /organizations/{organization_id}/answers."""

from fastapi import APIRouter
from pydantic import AwareDatetime, Field

from app.api.dependencies import (
    GeneratorDep,
    ProviderDep,
    RetrievalCacheDep,
    SessionDep,
    SettingsDep,
    TenantDep,
)
from app.api.errors import problem_responses
from app.domain.facts import PrivacyScope
from app.domain.tenancy import TenantContext
from app.schemas.api import Request
from app.services.answers import (
    MAX_QUESTION_CHARS,
    AnswerQuery,
    GroundedAnswer,
    GroundedAnswerService,
)
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["answers"])


class AnswerRequest(Request):
    question: str = Field(
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        examples=["What was customer-991's credit limit at 11:00 on 15 January?"],
    )
    valid_at: AwareDatetime | None = Field(
        default=None, description="Answer as of this time (default: now)."
    )
    known_at: AwareDatetime | None = Field(
        default=None, description="Use only what had been recorded by then (default: all)."
    )
    limit: int = Field(default=8, ge=1, le=20, description="Maximum facts given to the model.")
    entity_type: str | None = None
    external_ids: list[str] = Field(default_factory=list, max_length=100)
    properties: list[str] = Field(default_factory=list, max_length=100)
    max_privacy_scope: PrivacyScope | None = Field(
        default=None, description="Can only narrow what your role may see."
    )

    def to_query(self) -> AnswerQuery:
        return AnswerQuery(
            question=self.question,
            valid_at=self.valid_at,
            known_at=self.known_at,
            limit=self.limit,
            entity_type=self.entity_type,
            external_ids=tuple(self.external_ids),
            properties=tuple(self.properties),
            max_privacy_scope=self.max_privacy_scope,
        )


@router.post(
    "/answers",
    summary="Answer a question from authorized facts, with citations",
    responses=problem_responses(401, 403, 422, 429, 503),
)
async def answer(
    body: AnswerRequest,
    ctx: TenantDep,
    session: SessionDep,
    embeddings: ProviderDep,
    cache: RetrievalCacheDep,
    generator: GeneratorDep,
    settings: SettingsDep,
) -> GroundedAnswer:
    """Retrieves the facts you may see (tenant, role, privacy ceiling, `valid_at`, `known_at`),
    asks the configured LLM to answer from them only, and verifies every citation against
    the facts it was given. `status` is `answered`, `insufficient_evidence` (no model call
    when nothing is retrievable) or `ungrounded` (the answer is withheld). A model failure
    is a 503, never an invented answer."""

    async def retrieve(tenant: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        return await RetrievalService(session, embeddings, cache=cache).search(tenant, query)

    service = GroundedAnswerService(
        retrieve,
        generator,
        prompt_version=settings.llm_answer_prompt_version,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
    )
    return await service.answer(ctx, body.to_query())
