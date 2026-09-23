"""Hybrid temporal retrieval against real PostgreSQL + pgvector."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Text, cast, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.errors import PermissionDeniedError, ValidationFailedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.models.search import FactSearchDocument
from app.models.source import FactSource
from app.providers.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingBatch,
    EmbeddingProviderError,
)
from app.services.facts import RecordFactVersion
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService
from app.workers.embeddings import EmbeddingWorker
from tests.integration.factories import (
    add_member,
    admin_workspace,
    context,
    make_source,
    make_user,
    record,
    unique,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROVIDER = DeterministicHashEmbeddingProvider()


class RecordingProvider:
    """Deterministic provider that counts calls and can simulate an outage."""

    max_batch_size = 512

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    @property
    def model_id(self) -> str:
        return PROVIDER.model_id

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls += 1
        if self.fail:
            raise EmbeddingProviderError("simulated outage")
        return await PROVIDER.embed(texts)


async def fact(
    sessions: Sessions,
    ctx: TenantContext,
    source: FactSource,
    external_id: str,
    prop: str,
    value: object,
    *,
    hours: int = 0,
    **extra: Any,
) -> None:
    await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id=external_id,
            property=prop,
            value=value,
            source_id=source.id,
            valid_from=T0 + timedelta(hours=hours),
            **extra,
        ),
    )


async def embed_all(sessions: Sessions, ctx: TenantContext) -> None:
    worker = EmbeddingWorker(sessions, PROVIDER, organization_id=ctx.organization_id)
    while (await worker.run_once()).claimed:
        pass


async def search(
    sessions: Sessions, ctx: TenantContext, query: str, provider: Any = PROVIDER, **kwargs: Any
) -> RetrievalResult:
    async with sessions() as session:
        return await RetrievalService(session, provider).search(
            ctx, RetrievalQuery(query=query, **kwargs)
        )


def summary(result: RetrievalResult) -> list[tuple[str, str, object]]:
    return [(r.external_id, r.property, r.version.value) for r in result.results]


async def customers(sessions: Sessions) -> tuple[TenantContext, FactSource, str, str]:
    """Two customers with a credit limit, a shipping city and a status each."""
    ctx, source = await admin_workspace(sessions)
    alpha, beta = unique("customer"), unique("customer")
    for external_id, limit, city in ((alpha, 2000, "Berlin"), (beta, 7500, "Lisbon")):
        await fact(sessions, ctx, source, external_id, "credit_limit", limit)
        await fact(sessions, ctx, source, external_id, "shipping_city", city)
        await fact(sessions, ctx, source, external_id, "account_status", "active")
    await embed_all(sessions, ctx)
    return ctx, source, alpha, beta


# --- the search document ---------------------------------------------------------


async def test_every_version_gets_a_search_document(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "customer-991", "billing.credit_limit", 5000)

    async with sessions() as session:
        document = await session.scalar(
            select(cast(FactSearchDocument.search_vector, Text)).where(
                FactSearchDocument.organization_id == ctx.organization_id
            )
        )

    assert document is not None
    # The english parser splits "customer-991" into the word "customer" (stemmed to
    # "custom") and the signed integer "-991".
    for lexeme in ("'custom'", "'-991'", "'bill'", "'credit'", "'limit'", "'5000'"):
        assert lexeme in document


async def test_search_documents_are_append_only(engine: AsyncEngine, sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "customer-991", "credit_limit", 5000)

    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM fact_search_documents WHERE organization_id = :o"),
                {"o": ctx.organization_id},
            )


# --- relevance ---------------------------------------------------------------------


async def test_finds_the_relevant_fact_with_both_branches(sessions: Sessions) -> None:
    ctx, _, alpha, _ = await customers(sessions)

    result = await search(sessions, ctx, f"What is the credit limit of {alpha}?", limit=3)

    top = result.results[0]
    assert (top.external_id, top.property, top.version.value) == (alpha, "credit_limit", 2000)
    assert top.ranking.vector_rank is not None
    assert top.ranking.text_rank == 1
    assert result.vector_search == "used"
    assert result.embedding_model == PROVIDER.model_id
    assert top.source_name.startswith("source-")


async def test_keyword_only_match_is_found(sessions: Sessions) -> None:
    ctx, _, _, beta = await customers(sessions)

    result = await search(sessions, ctx, "Lisbon", limit=1)

    assert summary(result) == [(beta, "shipping_city", "Lisbon")]


async def test_unembedded_versions_are_still_found_by_full_text(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "customer-7", "credit_limit", 1200)  # no worker run

    result = await search(sessions, ctx, "credit limit customer-7")

    assert result.vector_candidates == 0
    assert summary(result) == [("customer-7", "credit_limit", 1200)]


async def test_provider_outage_degrades_to_full_text(sessions: Sessions) -> None:
    ctx, _, alpha, _ = await customers(sessions)
    provider = RecordingProvider(fail=True)

    result = await search(sessions, ctx, f"credit limit {alpha}", provider=provider)

    assert result.vector_search == "unavailable"
    assert result.vector_candidates == 0
    assert summary(result)[0] == (alpha, "credit_limit", 2000)


async def test_vectors_of_another_model_are_never_compared(sessions: Sessions) -> None:
    ctx, _, _, _ = await customers(sessions)

    class OtherModel(RecordingProvider):
        @property
        def model_id(self) -> str:
            return "deterministic:some-other-model"

    result = await search(sessions, ctx, "credit limit", provider=OtherModel())

    assert result.vector_candidates == 0
    assert result.text_candidates > 0


# --- time -------------------------------------------------------------------------


async def test_returns_the_version_valid_at_the_requested_time(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "customer-991", "credit_limit", 2000)
    await fact(sessions, ctx, source, "customer-991", "credit_limit", 5000, hours=5)
    await embed_all(sessions, ctx)

    past = await search(sessions, ctx, "credit limit", valid_at=T0 + timedelta(hours=1))
    now = await search(sessions, ctx, "credit limit")
    before_anything = await search(sessions, ctx, "credit limit", valid_at=T0 - timedelta(days=1))

    assert summary(past) == [("customer-991", "credit_limit", 2000)]
    assert summary(now) == [("customer-991", "credit_limit", 5000)]
    assert before_anything.results == []


async def test_as_known_at_hides_later_knowledge(sessions: Sessions, engine: AsyncEngine) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "customer-991", "credit_limit", 2000)
    async with engine.connect() as connection:
        known_at = await connection.scalar(text("SELECT clock_timestamp()"))
    await fact(sessions, ctx, source, "customer-991", "credit_limit", 5000, hours=5)

    then = await search(
        sessions, ctx, "credit limit", valid_at=T0 + timedelta(hours=6), known_at=known_at
    )
    later = await search(sessions, ctx, "credit limit", valid_at=T0 + timedelta(hours=6))

    assert summary(then) == [("customer-991", "credit_limit", 2000)]
    assert then.results[0].version.valid_until is None  # its end was not known yet
    assert summary(later) == [("customer-991", "credit_limit", 5000)]


# --- tenants, privacy, permissions ------------------------------------------------


async def test_other_tenants_facts_are_invisible(sessions: Sessions) -> None:
    ctx_a, _, alpha, beta = await customers(sessions)
    ctx_b, source_b = await admin_workspace(sessions)
    await fact(sessions, ctx_b, source_b, alpha, "credit_limit", 999_999)
    await embed_all(sessions, ctx_b)

    result = await search(sessions, ctx_a, f"credit limit {alpha}", limit=50)

    assert all(r.external_id in {alpha, beta} for r in result.results)
    assert 999_999 not in [r.version.value for r in result.results]


async def test_privacy_scope_follows_the_current_role(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    for scope in PrivacyScope:
        await fact(
            sessions,
            ctx,
            source,
            f"customer-{scope.lower()}",
            "credit_limit",
            1,
            privacy_scope=scope,
        )
    viewer, engineer = await make_user(sessions), await make_user(sessions)
    await add_member(sessions, ctx, viewer, MembershipRole.VIEWER)
    await add_member(sessions, ctx, engineer, MembershipRole.ENGINEER)

    async def seen(actor: TenantContext, **kwargs: Any) -> set[PrivacyScope]:
        result = await search(sessions, actor, "credit limit", limit=10, **kwargs)
        return {r.version.privacy_scope for r in result.results}

    viewer_ctx = await context(sessions, ctx.organization_id, viewer.id)
    engineer_ctx = await context(sessions, ctx.organization_id, engineer.id)
    assert await seen(viewer_ctx) == {PrivacyScope.PUBLIC, PrivacyScope.INTERNAL}
    assert await seen(engineer_ctx) == {
        PrivacyScope.PUBLIC,
        PrivacyScope.INTERNAL,
        PrivacyScope.CONFIDENTIAL,
    }
    assert await seen(ctx) == set(PrivacyScope)
    assert await seen(ctx, max_privacy_scope=PrivacyScope.PUBLIC) == {PrivacyScope.PUBLIC}
    # A VIEWER asking for RESTRICTED still gets only what the role allows.
    assert await seen(viewer_ctx, max_privacy_scope=PrivacyScope.RESTRICTED) == {
        PrivacyScope.PUBLIC,
        PrivacyScope.INTERNAL,
    }


async def test_non_members_are_rejected_before_any_embedding(sessions: Sessions) -> None:
    ctx, _, _, _ = await customers(sessions)
    outsider = await make_user(sessions)
    stranger = TenantContext(ctx.organization_id, outsider.id, MembershipRole.ADMIN)
    provider = RecordingProvider()

    with pytest.raises(PermissionDeniedError):
        await search(sessions, stranger, "credit limit", provider=provider)
    assert provider.calls == 0


# --- metadata filters and trust ---------------------------------------------------


async def test_metadata_filters(sessions: Sessions) -> None:
    ctx, source, alpha, beta = await customers(sessions)
    weak = await make_source(sessions, ctx, default_authority=20)
    await fact(sessions, ctx, weak, "customer-rumour", "credit_limit", 50_000, confidence="0.4")
    await embed_all(sessions, ctx)

    by_property = await search(sessions, ctx, alpha, properties=["shipping_city"], limit=1)
    by_entity = await search(sessions, ctx, "credit limit", external_ids=[beta])
    by_source = await search(sessions, ctx, "credit limit", source_ids=[weak.id])
    trusted = await search(sessions, ctx, "credit limit", min_authority=50, limit=50)
    confident = await search(sessions, ctx, "credit limit", min_confidence=0.9, limit=50)
    other_type = await search(sessions, ctx, "credit limit", entity_type="vendor")

    assert summary(by_property) == [(alpha, "shipping_city", "Berlin")]
    assert {r.external_id for r in by_entity.results} == {beta}
    assert summary(by_source) == [("customer-rumour", "credit_limit", 50_000)]
    assert "customer-rumour" not in {r.external_id for r in trusted.results}
    assert "customer-rumour" not in {r.external_id for r in confident.results}
    assert other_type.results == []
    assert all(r.source_name == source.name for r in trusted.results)


async def test_authority_breaks_near_ties(sessions: Sessions) -> None:
    ctx, _ = await admin_workspace(sessions)
    weak = await make_source(sessions, ctx, default_authority=10)
    strong = await make_source(sessions, ctx, default_authority=100)
    await fact(sessions, ctx, weak, "customer-a", "credit_limit", 1000)
    await fact(sessions, ctx, strong, "customer-b", "credit_limit", 1000)
    await embed_all(sessions, ctx)

    ranked = await search(sessions, ctx, "credit limit", trust_weight=1.0)

    assert [r.external_id for r in ranked.results] == ["customer-b", "customer-a"]
    assert ranked.results[0].ranking.trust == pytest.approx(1.0)
    assert ranked.results[1].ranking.trust == pytest.approx(0.1)


# --- validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "   "},
        {"query": "x", "limit": 0},
        {"query": "x", "valid_at": datetime(2026, 1, 1)},  # noqa: DTZ001 (deliberately naive)
        {"query": "x", "min_authority": 101},
        {"query": "x", "min_confidence": 2},
        {"query": "x", "properties": ["Not A Property!"]},
        {"query": "x", "trust_weight": 2.0},
    ],
)
async def test_invalid_requests_are_rejected(sessions: Sessions, kwargs: dict[str, Any]) -> None:
    ctx, _ = await admin_workspace(sessions)
    provider = RecordingProvider()

    with pytest.raises(ValidationFailedError):
        async with sessions() as session:
            await RetrievalService(session, provider).search(ctx, RetrievalQuery(**kwargs))
    assert provider.calls == 0
