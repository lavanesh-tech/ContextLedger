"""GroundedAnswerService with a scripted retriever and a fake model (no database, no network)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from app.ai.grounding import AnswerStatus
from app.ai.providers import (
    FakeGenerationProvider,
    GenerationRateLimitedError,
    GenerationRequest,
)
from app.domain.errors import PermissionDeniedError, ValidationFailedError
from app.domain.facts import PrivacyScope, SourceType
from app.domain.retrieval import RankedCandidate
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.services.answers import AnswerGenerationError, AnswerQuery, GroundedAnswerService
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievedFact
from app.temporal.model import VersionSnapshot

T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
CTX = TenantContext(organization_id=UUID(int=1), user_id=UUID(int=2), role=MembershipRole.ENGINEER)


def retrieved(n: int, value: Any, *, hours: int = 0) -> RetrievedFact:
    version = VersionSnapshot(
        id=UUID(int=10 + n),
        fact_id=UUID(int=99),
        version=n,
        value=value,
        source_id=UUID(int=50),
        valid_from=T0 + timedelta(hours=hours),
        valid_until=None,
        observed_at=T0,
        recorded_at=T0,
        valid_until_recorded_at=None,
        supersedes_id=None,
        authority=90,
        confidence=Decimal("1.000"),
        privacy_scope=PrivacyScope.INTERNAL,
    )
    ranking = RankedCandidate(version.id, n, 0.1, None, None, 0.01, 0.9, 0.02)
    return RetrievedFact(
        version, "customer", "customer-991", "credit_limit", "billing-db", SourceType.API, ranking
    )


class Retriever:
    def __init__(self, *facts: RetrievedFact, error: Exception | None = None) -> None:
        self.facts = list(facts)
        self.error = error
        self.queries: list[RetrievalQuery] = []

    async def __call__(self, ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return RetrievalResult(
            query=query.query,
            valid_at=query.valid_at or T0,
            known_at=query.known_at,
            vector_search="used",
            embedding_model="deterministic:hash-v1",
            privacy_scopes=(PrivacyScope.PUBLIC, PrivacyScope.INTERNAL),
            vector_candidates=len(self.facts),
            text_candidates=0,
            results=self.facts,
        )


def reply(answer: str = "5000", cited: list[str] | None = None, **extra: Any) -> str:
    return json.dumps(
        {
            "answer": answer,
            "insufficient_evidence": extra.get("insufficient", False),
            "cited_facts": ["F1"] if cited is None else cited,
            "inferences": extra.get("inferences", []),
        }
    )


def service(
    retriever: Retriever, model: FakeGenerationProvider, version: str = "grounded-answer-v2"
) -> GroundedAnswerService:
    return GroundedAnswerService(retriever, model, prompt_version=version, max_output_tokens=300)


def user_prompt(request: GenerationRequest) -> str:
    return request.messages[-1].content


async def test_a_grounded_answer_with_resolved_citations() -> None:
    retriever = Retriever(retrieved(1, 5000))
    model = FakeGenerationProvider(responses=[reply(inferences=["stable since January"])])

    result = await service(retriever, model).answer(
        CTX, AnswerQuery("What is the credit limit of customer-991?")
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.grounded and not result.insufficient_evidence
    assert result.answer == "5000"
    [citation] = result.citations
    assert citation.label == "F1" and citation.fact_version_id == UUID(int=11)
    assert citation.source_id == UUID(int=50) and citation.source_name == "billing-db"
    assert result.inferences == ["stable since January"]
    assert result.supplied_fact_version_ids == [UUID(int=11)]
    assert result.generation is not None
    assert result.generation.prompt_version == "grounded-answer-v2"
    assert result.generation.provider == "fake" and result.generation.input_tokens == 20
    assert result.retrieval.facts_supplied == 1


async def test_the_request_carries_the_prompt_the_time_and_only_supplied_facts() -> None:
    retriever = Retriever(retrieved(1, 2000))
    model = FakeGenerationProvider(responses=[reply()])
    valid_at = T0 + timedelta(hours=1)

    await service(retriever, model).answer(
        CTX, AnswerQuery("limit at 10am?", valid_at=valid_at, limit=3)
    )

    [request] = model.requests
    assert request.messages[0].role == "system"
    assert "Never replace an older value" in request.messages[0].content
    prompt = user_prompt(request)
    assert "Question: limit at 10am?" in prompt
    assert f"(valid_at): {valid_at.isoformat()}" in prompt
    assert "[F1] customer/customer-991 credit_limit = 2000" in prompt
    assert "[F2]" not in prompt
    assert request.output_schema is not None and request.output_schema.name == "grounded_answer"
    assert request.max_output_tokens == 300
    assert retriever.queries[0].valid_at == valid_at and retriever.queries[0].limit == 3


async def test_no_authorized_facts_means_no_model_call() -> None:
    model = FakeGenerationProvider()

    result = await service(Retriever(), model).answer(CTX, AnswerQuery("anything?"))

    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.generation is None and model.requests == []
    assert result.citations == []


async def test_authorization_failures_stop_before_the_model() -> None:
    model = FakeGenerationProvider(responses=[reply()])
    retriever = Retriever(error=PermissionDeniedError("not a member of this organization"))

    with pytest.raises(PermissionDeniedError):
        await service(retriever, model).answer(CTX, AnswerQuery("limit?"))
    assert model.requests == []


async def test_the_model_saying_insufficient_evidence_is_kept() -> None:
    model = FakeGenerationProvider(
        responses=[reply("The facts do not mention a payment date.", cited=[], insufficient=True)]
    )

    result = await service(Retriever(retrieved(1, 5000)), model).answer(
        CTX, AnswerQuery("When was the last payment?")
    )

    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result.answer == "The facts do not mention a payment date."
    assert result.citations == [] and result.generation is not None


async def test_invented_citations_withhold_the_answer() -> None:
    model = FakeGenerationProvider(responses=[reply("7500", cited=["F1", "F4"])])

    result = await service(Retriever(retrieved(1, 5000)), model).answer(CTX, AnswerQuery("limit?"))

    assert result.status is AnswerStatus.UNGROUNDED
    assert result.answer is None and not result.grounded
    assert result.rejected_citations == ["F4"] and result.citations == []


async def test_an_uncited_answer_is_withheld() -> None:
    model = FakeGenerationProvider(responses=[reply("7500", cited=[])])

    result = await service(Retriever(retrieved(1, 5000)), model).answer(CTX, AnswerQuery("limit?"))

    assert result.status is AnswerStatus.UNGROUNDED and result.answer is None


async def test_provider_failures_never_become_answers() -> None:
    model = FakeGenerationProvider(responses=[GenerationRateLimitedError("quota")])

    with pytest.raises(AnswerGenerationError) as caught:
        await service(Retriever(retrieved(1, 5000)), model).answer(CTX, AnswerQuery("limit?"))
    assert caught.value.cause == "GenerationRateLimitedError"


@pytest.mark.parametrize("text", ["not json", '{"answer": "5000"}', '["F1"]'])
async def test_malformed_model_output_is_an_error_not_an_answer(text: str) -> None:
    model = FakeGenerationProvider(responses=[text])

    with pytest.raises(AnswerGenerationError) as caught:
        await service(Retriever(retrieved(1, 5000)), model).answer(CTX, AnswerQuery("limit?"))
    assert caught.value.cause == "MalformedGenerationError"


@pytest.mark.parametrize("question", ["", "   ", "x" * 1001])
async def test_questions_are_validated(question: str) -> None:
    with pytest.raises(ValidationFailedError):
        await service(Retriever(), FakeGenerationProvider()).answer(CTX, AnswerQuery(question))


async def test_the_prompt_version_is_selectable() -> None:
    model = FakeGenerationProvider(responses=[reply()])

    result = await service(Retriever(retrieved(1, 5000)), model, "grounded-answer-v1").answer(
        CTX, AnswerQuery("limit?")
    )

    assert result.generation is not None
    assert result.generation.prompt_version == "grounded-answer-v1"
    assert "Never replace an older value" not in model.requests[0].messages[0].content
