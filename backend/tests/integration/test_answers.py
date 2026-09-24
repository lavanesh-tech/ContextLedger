"""Grounded answers against real PostgreSQL retrieval, with a recording fake model.

These tests look at exactly what WOULD be sent to a model: authorization, privacy
and time filtering must already have removed everything the caller may not see.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.grounding import AnswerStatus
from app.ai.providers import FakeGenerationProvider, GenerationRequest
from app.domain.errors import PermissionDeniedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.services.answers import AnswerQuery, GroundedAnswerService
from app.services.memberships import MembershipService
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService
from tests.integration.factories import add_member, admin_workspace, context, make_user
from tests.integration.test_decisions import fact

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROVIDER = DeterministicHashEmbeddingProvider()


def cite_first(request: GenerationRequest) -> str:
    return json.dumps(
        {
            "answer": "see F1",
            "insufficient_evidence": False,
            "cited_facts": ["F1"],
            "inferences": [],
        }
    )


def answers(sessions: Sessions, model: FakeGenerationProvider) -> GroundedAnswerService:
    async def retrieve(ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        async with sessions() as session:
            return await RetrievalService(session, PROVIDER).search(ctx, query)

    return GroundedAnswerService(retrieve, model, prompt_version="grounded-answer-v2")


def prompt_of(model: FakeGenerationProvider) -> str:
    [request] = model.requests
    return request.messages[-1].content


async def test_a_historical_question_sees_the_version_valid_then(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    old = await fact(sessions, ctx, source, "credit_limit", 2000)
    await fact(sessions, ctx, source, "credit_limit", 5000, hours=5)
    model = FakeGenerationProvider(responder=cite_first)

    result = await answers(sessions, model).answer(
        ctx,
        AnswerQuery("credit limit of customer-991", valid_at=T0 + timedelta(hours=1)),
    )

    prompt = prompt_of(model)
    assert "credit_limit = 2000" in prompt
    assert "credit_limit = 5000" not in prompt
    assert [c.fact_version_id for c in result.citations] == [old.id]
    assert result.status is AnswerStatus.ANSWERED


async def test_the_current_question_sees_the_newest_version(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 2000)
    new = await fact(sessions, ctx, source, "credit_limit", 5000, hours=5)
    model = FakeGenerationProvider(responder=cite_first)

    result = await answers(sessions, model).answer(ctx, AnswerQuery("credit limit of customer-991"))

    assert "credit_limit = 5000" in prompt_of(model)
    assert "credit_limit = 2000" not in prompt_of(model)
    assert [c.fact_version_id for c in result.citations] == [new.id]


async def test_facts_above_the_callers_privacy_ceiling_never_reach_the_model(
    sessions: Sessions,
) -> None:
    admin_ctx, source = await admin_workspace(sessions)
    await fact(sessions, admin_ctx, source, "credit_limit", 5000)
    await fact(
        sessions,
        admin_ctx,
        source,
        "risk_notes",
        "fraud review open",
        privacy_scope=PrivacyScope.CONFIDENTIAL,
    )
    viewer = await make_user(sessions)
    await add_member(sessions, admin_ctx, viewer, MembershipRole.VIEWER)
    viewer_ctx = await context(sessions, admin_ctx.organization_id, viewer.id)
    model = FakeGenerationProvider(responder=cite_first)

    await answers(sessions, model).answer(viewer_ctx, AnswerQuery("customer-991 risk notes credit"))

    prompt = prompt_of(model)
    assert "fraud review open" not in prompt and "risk_notes" not in prompt
    assert "credit_limit = 5000" in prompt


async def test_an_agents_ceiling_narrows_what_reaches_the_model(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(
        sessions, ctx, source, "risk_notes", "watch", privacy_scope=PrivacyScope.CONFIDENTIAL
    )
    model = FakeGenerationProvider(responder=cite_first)

    result = await answers(sessions, model).answer(
        ctx, AnswerQuery("customer-991 risk notes", max_privacy_scope=PrivacyScope.INTERNAL)
    )

    assert result.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert model.requests == []  # nothing authorized: no model call at all


async def test_another_tenants_facts_never_reach_the_model(sessions: Sessions) -> None:
    victim, victim_source = await admin_workspace(sessions)
    await fact(sessions, victim, victim_source, "credit_limit", 987654)
    attacker, attacker_source = await admin_workspace(sessions)
    await fact(sessions, attacker, attacker_source, "credit_limit", 1000)
    model = FakeGenerationProvider(responder=cite_first)

    await answers(sessions, model).answer(attacker, AnswerQuery("credit limit customer-991"))

    assert "987654" not in prompt_of(model)
    assert "credit_limit = 1000" in prompt_of(model)


async def test_a_removed_member_is_refused_before_any_model_call(sessions: Sessions) -> None:
    admin_ctx, source = await admin_workspace(sessions)
    await fact(sessions, admin_ctx, source, "credit_limit", 5000)
    member = await make_user(sessions)
    await add_member(sessions, admin_ctx, member, MembershipRole.ENGINEER)
    member_ctx = await context(sessions, admin_ctx.organization_id, member.id)
    async with sessions() as session:
        await MembershipService(session).remove_member(admin_ctx, user_id=member.id)
    model = FakeGenerationProvider(responder=cite_first)

    with pytest.raises(PermissionDeniedError):
        await answers(sessions, model).answer(member_ctx, AnswerQuery("credit limit"))
    assert model.requests == []
