"""Embedding worker, job queue and vector storage against real PostgreSQL + pgvector."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.embeddings import EmbeddingJobStatus
from app.domain.tenancy import TenantContext
from app.models.embedding import EmbeddingJob, FactEmbedding
from app.models.fact import FactVersion
from app.providers.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingBatch,
    EmbeddingProviderError,
)
from app.repositories.embeddings import EmbeddingJobStore, EmbeddingRepository
from app.services.facts import RecordFactVersion
from app.workers.embeddings import EmbeddingWorker
from tests.integration.factories import admin_workspace, record, unique

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


class CountingProvider:
    """Deterministic vectors, but counts what was sent (and can be told to fail)."""

    max_batch_size = 512

    def __init__(self, *, fail: bool = False) -> None:
        self._inner = DeterministicHashEmbeddingProvider()
        self.fail = fail
        self.texts: list[str] = []

    @property
    def model_id(self) -> str:
        return "test:counting"

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if self.fail:
            raise EmbeddingProviderError("simulated outage")
        self.texts.extend(texts)
        return await self._inner.embed(texts)


def worker(sessions: Sessions, provider: Any, ctx: TenantContext, **kwargs: Any) -> EmbeddingWorker:
    return EmbeddingWorker(
        sessions,
        provider,
        batch_size=kwargs.pop("batch_size", 50),
        retry_base_seconds=kwargs.pop("retry_base_seconds", 0.0),
        organization_id=ctx.organization_id,
        **kwargs,
    )


@dataclass(frozen=True, slots=True)
class Story:
    ctx: TenantContext
    source_id: UUID
    customer: str
    credit_limit_ids: list[UUID]


async def _record(
    sessions: Sessions,
    ctx: TenantContext,
    story: tuple[UUID, str],
    prop: str,
    value: object,
    hours: int,
) -> FactVersion:
    source_id, customer = story
    return await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id=customer,
            property=prop,
            value=value,
            source_id=source_id,
            valid_from=T0 + timedelta(hours=hours),
        ),
    )


async def _story(sessions: Sessions, values: Sequence[object] = (2000, 5000)) -> Story:
    """One customer: a credit_limit version per value (hourly) plus one shipping_city."""
    ctx, source = await admin_workspace(sessions)
    key = (source.id, unique("customer"))
    ids = [
        (await _record(sessions, ctx, key, "credit_limit", value, hours)).id
        for hours, value in enumerate(values)
    ]
    await _record(sessions, ctx, key, "shipping_city", "Berlin", 0)
    return Story(ctx=ctx, source_id=key[0], customer=key[1], credit_limit_ids=ids)


async def _jobs(sessions: Sessions, ctx: TenantContext) -> list[EmbeddingJob]:
    async with sessions() as session:
        result = await session.scalars(
            select(EmbeddingJob).where(EmbeddingJob.organization_id == ctx.organization_id)
        )
        return list(result.all())


async def _embedding_count(sessions: Sessions, ctx: TenantContext, model: str) -> int:
    async with sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(FactEmbedding)
            .where(
                FactEmbedding.organization_id == ctx.organization_id,
                FactEmbedding.model == model,
            )
        )
    return int(count or 0)


async def test_worker_embeds_every_version_once(sessions: Sessions) -> None:
    ctx = (await _story(sessions)).ctx
    provider = CountingProvider()

    first = await worker(sessions, provider, ctx).run_once()
    second = await worker(sessions, provider, ctx).run_once()

    assert (first.enqueued, first.claimed, first.succeeded) == (3, 3, 3)
    assert (second.enqueued, second.claimed) == (0, 0)  # idempotent
    assert await _embedding_count(sessions, ctx, provider.model_id) == 3
    assert {j.status for j in await _jobs(sessions, ctx)} == {EmbeddingJobStatus.SUCCEEDED}


async def test_identical_text_reuses_the_tenants_existing_vector(sessions: Sessions) -> None:
    story = await _story(sessions, values=(2000,))
    provider = CountingProvider()
    await worker(sessions, provider, story.ctx).run_once()
    sent_before = len(provider.texts)

    # 2000 -> 5000 -> 2000: the third version renders exactly like the first.
    key = (story.source_id, story.customer)
    for hours, value in ((5, 5000), (6, 2000)):
        await _record(sessions, story.ctx, key, "credit_limit", value, hours)
    stats = await worker(sessions, provider, story.ctx).run_once()

    assert stats.succeeded == 2
    assert stats.reused == 1  # the repeated "2000" text
    assert len(provider.texts) - sent_before == 1  # only "5000" went to the provider


async def test_provider_failure_backs_off_then_fails_permanently(sessions: Sessions) -> None:
    ctx = (await _story(sessions, values=(2000,))).ctx
    provider = CountingProvider(fail=True)
    failing = worker(sessions, provider, ctx, max_attempts=2)

    first = await failing.run_once()
    jobs = await _jobs(sessions, ctx)
    assert first.failed == 0
    assert {(j.status, j.attempts) for j in jobs} == {(EmbeddingJobStatus.PENDING, 1)}
    assert all("simulated outage" in (j.last_error or "") for j in jobs)
    assert all(j.lease_token is None for j in jobs)

    second = await failing.run_once()  # base delay 0 -> immediately due again
    assert second.failed == 2
    assert {j.status for j in await _jobs(sessions, ctx)} == {EmbeddingJobStatus.FAILED}

    third = await failing.run_once()  # FAILED jobs are never picked up again
    assert third.claimed == 0


async def test_backoff_delays_the_next_attempt(sessions: Sessions) -> None:
    ctx = (await _story(sessions, values=(2000,))).ctx
    provider = CountingProvider(fail=True)

    await worker(sessions, provider, ctx, retry_base_seconds=3600).run_once()
    provider.fail = False
    stats = await worker(sessions, provider, ctx).run_once()

    assert stats.claimed == 0  # not due for another hour


async def test_concurrent_workers_never_process_the_same_job(sessions: Sessions) -> None:
    ctx = (await _story(sessions, values=tuple(range(12)))).ctx
    providers = [CountingProvider() for _ in range(3)]

    results = await asyncio.gather(
        *(worker(sessions, p, ctx, batch_size=5).run_once() for p in providers)
    )
    for _ in range(10):  # drain whatever the racing workers left behind
        drained = await worker(sessions, providers[0], ctx, batch_size=5).run_once()
        if drained.claimed == 0:
            break

    jobs = await _jobs(sessions, ctx)
    assert len(jobs) == 13
    assert {j.status for j in jobs} == {EmbeddingJobStatus.SUCCEEDED}
    assert all(j.attempts == 1 for j in jobs)  # nobody processed a job twice
    assert sum(r.claimed for r in results) <= 13
    sent = [text for p in providers for text in p.texts]
    assert len(sent) == len(set(sent)) == 13  # every distinct text embedded exactly once


async def test_a_crashed_workers_lease_expires_and_is_reclaimed(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    ctx = (await _story(sessions, values=(2000,))).ctx
    provider = CountingProvider()
    async with sessions() as session, session.begin():
        store = EmbeddingJobStore(session, model=provider.model_id)
        await store.enqueue_missing(limit=10, organization_id=ctx.organization_id)
        crashed = await store.claim(
            batch_size=10, lease=timedelta(minutes=5), organization_id=ctx.organization_id
        )
    assert len(crashed) == 2

    still_leased = await worker(sessions, provider, ctx).run_once()
    assert still_leased.claimed == 0

    async with engine.begin() as connection:
        await connection.execute(
            update(EmbeddingJob)
            .where(EmbeddingJob.organization_id == ctx.organization_id)
            .values(locked_at=func.now() - timedelta(hours=1))
        )
    reclaimed = await worker(sessions, provider, ctx).run_once()

    assert reclaimed.succeeded == 2
    assert {j.attempts for j in await _jobs(sessions, ctx)} == {2}

    # The crashed worker's late completion must not overwrite anything.
    async with sessions() as session, session.begin():
        late = EmbeddingJobStore(session, model=provider.model_id)
        assert await late.complete(crashed[0]) is False


async def test_nearest_neighbours_are_tenant_scoped_and_ranked(sessions: Sessions) -> None:
    a = await _story(sessions, values=(2000,))
    b = await _story(sessions, values=(2000,))
    provider = CountingProvider()
    await worker(sessions, provider, a.ctx).run_once()
    await worker(sessions, provider, b.ctx).run_once()
    query = (await provider.embed(["credit limit"])).vectors[0]

    async with sessions() as session:
        hits = await EmbeddingRepository(session, a.ctx.organization_id).nearest(
            model=provider.model_id, query=query, limit=10
        )
        own = await session.scalars(
            select(FactVersion.id).where(FactVersion.organization_id == a.ctx.organization_id)
        )
        own_versions = set(own.all())

    assert {version_id for version_id, _ in hits} <= own_versions
    assert len(hits) == 2
    assert hits[0][0] == a.credit_limit_ids[0]  # "credit limit" beats "shipping city"
    assert hits[0][1] <= hits[1][1]


# --- database guarantees ------------------------------------------------------------


async def test_embedding_dimensions_are_enforced(engine: AsyncEngine, sessions: Sessions) -> None:
    story = await _story(sessions, values=(2000,))

    with pytest.raises(DBAPIError, match="dimensions"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO fact_embeddings (organization_id, fact_version_id, model, "
                    "dimensions, text_template, content_sha256, embedding) "
                    "VALUES (:o, :v, 'x', 1536, 't', repeat('a', 64), '[1,2,3]')"
                ),
                {"o": story.ctx.organization_id, "v": story.credit_limit_ids[0]},
            )


async def test_running_jobs_must_hold_a_lease(engine: AsyncEngine, sessions: Sessions) -> None:
    ctx = (await _story(sessions, values=(2000,))).ctx
    await worker(sessions, CountingProvider(fail=True), ctx).run_once()

    with pytest.raises(IntegrityError, match="ck_embedding_jobs_lease_only_while_running"):
        async with engine.begin() as connection:
            await connection.execute(
                update(EmbeddingJob)
                .where(EmbeddingJob.organization_id == ctx.organization_id)
                .values(status=EmbeddingJobStatus.RUNNING)
            )


async def test_hnsw_cosine_index_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        definition = await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ann_fact_embeddings_embedding_cosine'"
            )
        )

    assert definition is not None
    assert "USING hnsw" in definition
    assert "vector_cosine_ops" in definition
