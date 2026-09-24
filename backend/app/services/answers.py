"""Grounded answers: "answer this question from the facts I am allowed to see, as of T".

Pipeline:

1. Retrieval through ``RetrievalService`` (unchanged). Tenant membership,
   permission, token scopes, privacy ceilings and the valid_at / known_at
   constraints are applied there, deterministically, before anything else.
2. No authorized facts: the answer is ``insufficient_evidence`` and NO model is
   called (cheaper, and nothing to hallucinate from).
3. The authorized facts are labelled F1..Fn and rendered into a versioned prompt.
4. A LangChain chain (ChatPromptTemplate | ContextLedgerChatModel) calls the
   generation provider with a strict JSON schema.
5. The structured output is validated, and its citations are checked against
   the facts that were actually supplied (``app/ai/grounding.py``).
6. The result separates the answer, the citations (resolved to fact versions and
   sources by our code, not by the model), retrieval metadata and generation
   metadata.

A provider failure raises ``AnswerGenerationError``: no answer is fabricated.
The model never decides tenant, permission, time, version or provenance.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage

from app.ai.grounding import (
    AnswerStatus,
    PackedFact,
    check_answer,
    pack_facts,
    parse_model_answer,
    render_facts,
)
from app.ai.orchestration.chains import grounded_answer_chain
from app.ai.orchestration.chat_model import ContextLedgerChatModel
from app.ai.prompts import get_prompt
from app.ai.providers import GenerationError, GenerationProvider, MalformedGenerationError
from app.domain.errors import ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.tenancy import TenantContext
from app.services.retrieval import RetrievalQuery, RetrievalResult

logger = logging.getLogger("contextledger.answers")

MAX_QUESTION_CHARS = 1000
Retrieve = Callable[[TenantContext, RetrievalQuery], Awaitable[RetrievalResult]]


class AnswerGenerationError(Exception):
    """The model could not produce a usable answer (provider or output failure)."""

    def __init__(self, message: str, *, cause: str) -> None:
        super().__init__(message)
        self.cause = cause  # the GenerationError class name, for callers and metrics


@dataclass(frozen=True, slots=True)
class Citation:
    label: str
    fact_version_id: UUID
    fact_id: UUID
    source_id: UUID
    source_name: str
    entity_type: str
    external_id: str
    property: str
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class RetrievalInfo:
    valid_at: datetime
    known_at: datetime | None
    vector_search: str
    privacy_scopes: tuple[PrivacyScope, ...]
    facts_supplied: int
    cache: str


@dataclass(frozen=True, slots=True)
class GenerationInfo:
    provider: str
    model: str
    prompt_version: str
    prompt_fingerprint: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    attempts: int
    finish_reason: str


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    question: str
    status: AnswerStatus
    answer: str | None  # None unless status is ANSWERED or INSUFFICIENT_EVIDENCE
    grounded: bool  # True only when every citation was verified against supplied facts
    insufficient_evidence: bool
    citations: list[Citation]
    inferences: list[str]
    rejected_citations: list[str]  # labels the model cited that were never supplied
    retrieval: RetrievalInfo
    generation: GenerationInfo | None  # None when no model was called
    supplied_fact_version_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AnswerQuery:
    question: str
    valid_at: datetime | None = None
    known_at: datetime | None = None
    limit: int = 8
    entity_type: str | None = None
    external_ids: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    max_privacy_scope: PrivacyScope | None = None


class GroundedAnswerService:
    def __init__(
        self,
        retrieve: Retrieve,
        generator: GenerationProvider,
        *,
        prompt_version: str,
        max_output_tokens: int = 800,
        temperature: float | None = 0.0,
    ) -> None:
        self._retrieve = retrieve
        self._prompt = get_prompt(prompt_version)
        self._chain = grounded_answer_chain(
            self._prompt,
            ContextLedgerChatModel(
                provider=generator, max_output_tokens=max_output_tokens, temperature=temperature
            ),
        )

    async def answer(self, ctx: TenantContext, query: AnswerQuery) -> GroundedAnswer:
        question = " ".join(query.question.split())
        if not question:
            raise ValidationFailedError("question must not be empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationFailedError(f"question must be at most {MAX_QUESTION_CHARS} characters")

        # 1. Authorization + time + privacy happen inside retrieval (deterministic).
        retrieval = await self._retrieve(
            ctx,
            RetrievalQuery(
                query=question,
                limit=query.limit,
                valid_at=query.valid_at,
                known_at=query.known_at,
                entity_type=query.entity_type,
                external_ids=query.external_ids,
                properties=query.properties,
                max_privacy_scope=query.max_privacy_scope,
            ),
        )
        facts = pack_facts(retrieval.results)
        info = RetrievalInfo(
            valid_at=retrieval.valid_at,
            known_at=retrieval.known_at,
            vector_search=retrieval.vector_search,
            privacy_scopes=retrieval.privacy_scopes,
            facts_supplied=len(facts),
            cache=retrieval.cache,
        )

        # 2. Nothing authorized to answer from: say so without calling a model.
        if not facts:
            return GroundedAnswer(
                question=question,
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                answer="No facts you are allowed to see match this question at that time.",
                grounded=True,
                insufficient_evidence=True,
                citations=[],
                inferences=[],
                rejected_citations=[],
                retrieval=info,
                generation=None,
            )

        # 3-4. Versioned prompt over the authorized facts only.
        # LangChain chain: ChatPromptTemplate | ContextLedgerChatModel (-> GenerationProvider).
        inputs = {
            "question": question,
            "valid_at": retrieval.valid_at.isoformat(),
            "known_at": (
                retrieval.known_at.isoformat()
                if retrieval.known_at
                else "everything recorded so far"
            ),
            "facts": render_facts(facts),
        }
        started = time.perf_counter()
        try:
            message = await self._chain.ainvoke(inputs)
            model_answer = parse_model_answer(_json_content(message))
        except GenerationError as exc:
            logger.warning(
                "answers.generation_failed",
                extra={
                    "organization_id": str(ctx.organization_id),
                    "prompt_version": self._prompt.version,
                    "error_type": type(exc).__name__,
                },
            )
            raise AnswerGenerationError(
                "the language model could not produce an answer", cause=type(exc).__name__
            ) from exc
        meta = message.response_metadata
        usage = getattr(message, "usage_metadata", None) or {}

        # 5. Deterministic citation check against the supplied facts.
        check = check_answer(model_answer, facts)
        generation = GenerationInfo(
            provider=str(meta.get("provider", "unknown")),
            model=str(meta.get("model", "unknown")),
            prompt_version=self._prompt.version,
            prompt_fingerprint=self._prompt.fingerprint,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            attempts=int(meta.get("attempts", 1)),
            finish_reason=str(meta.get("finish_reason", "unknown")),
        )
        logger.info(
            "answers.generated",
            extra={
                "organization_id": str(ctx.organization_id),
                "status": check.status.value,
                "facts_supplied": len(facts),
                "citations": len(check.cited),
                "rejected_citations": len(check.rejected_labels),
                "prompt_version": self._prompt.version,
                "model": generation.model,
                "input_tokens": generation.input_tokens,
                "output_tokens": generation.output_tokens,
            },
        )
        answered = check.status is AnswerStatus.ANSWERED
        return GroundedAnswer(
            question=question,
            status=check.status,
            answer=(model_answer.answer if check.status is not AnswerStatus.UNGROUNDED else None),
            grounded=check.status is not AnswerStatus.UNGROUNDED,
            insufficient_evidence=check.status is AnswerStatus.INSUFFICIENT_EVIDENCE,
            citations=[_citation(f) for f in check.cited],
            inferences=model_answer.inferences if answered else [],
            rejected_citations=check.rejected_labels,
            retrieval=info,
            generation=generation,
            supplied_fact_version_ids=[f.fact_version_id for f in facts],
        )


def _json_content(message: BaseMessage) -> Any:
    if not isinstance(message.content, str):
        raise MalformedGenerationError("the model returned non-text content")
    try:
        return json.loads(message.content)
    except json.JSONDecodeError as exc:
        raise MalformedGenerationError("the model did not return valid JSON") from exc


def _citation(fact: PackedFact) -> Citation:
    return Citation(
        label=fact.label,
        fact_version_id=fact.fact_version_id,
        fact_id=fact.fact_id,
        source_id=fact.source_id,
        source_name=fact.source_name,
        entity_type=fact.entity_type,
        external_id=fact.external_id,
        property=fact.property,
        valid_from=fact.valid_from,
        valid_until=fact.valid_until,
    )
