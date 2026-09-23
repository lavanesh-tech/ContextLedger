"""Background worker that turns fact versions into embeddings.

One iteration (``run_once``):

1. **Reconcile**: create jobs for versions that have neither an embedding nor a job.
2. **Claim**: lease a batch with ``FOR UPDATE SKIP LOCKED``, then commit, so
   the provider call happens *outside* any database transaction.
3. **Embed**: render each version's text, reuse vectors already computed for
   identical text in the same organization, and call the provider only for the rest.
4. **Write**: store embeddings and mark jobs SUCCEEDED in one transaction,
   guarded by the lease token.
5. **On provider failure**: retry with exponential backoff; FAILED after
   ``max_attempts``.

Run:  python -m app.workers.embeddings            (loop)
      python -m app.workers.embeddings --once     (single batch, e.g. cron / CI)
      python -m app.workers.embeddings --heartbeat-file /tmp/worker.heartbeat
                                                  (touch a file after every healthy
                                                   iteration, for container health checks)
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.domain.embeddings import (
    FACT_TEXT_TEMPLATE,
    document_hash,
    render_fact_document,
    retry_delay,
)
from app.providers.embeddings import (
    EmbeddingProvider,
    EmbeddingProviderError,
    build_embedding_provider,
    build_openai_http_client,
)
from app.repositories.embeddings import ClaimedJob, EmbeddingJobStore

logger = logging.getLogger("contextledger.worker.embeddings")


@dataclass(frozen=True, slots=True)
class WorkerStats:
    enqueued: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    reused: int = 0  # vectors taken from the per-tenant content-hash cache
    embedded: int = 0  # texts actually sent to the provider
    tokens: int = 0
    lease_lost: int = 0


class EmbeddingWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider,
        *,
        batch_size: int = 64,
        max_attempts: int = 5,
        lease: timedelta = timedelta(minutes=5),
        retry_base_seconds: float = 10.0,
        retry_cap_seconds: float = 900.0,
        organization_id: UUID | None = None,
    ) -> None:
        self._sessions = sessions
        self._provider = provider
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease = lease
        self._retry_base = retry_base_seconds
        self._retry_cap = retry_cap_seconds
        self._organization_id = organization_id  # optional scope, e.g. re-index one tenant

    @property
    def model(self) -> str:
        return self._provider.model_id

    def _store(self, session: AsyncSession) -> EmbeddingJobStore:
        return EmbeddingJobStore(session, model=self.model)

    async def run_once(self) -> WorkerStats:
        async with self._sessions() as session, session.begin():
            enqueued = await self._store(session).enqueue_missing(
                limit=self._batch_size * 4, organization_id=self._organization_id
            )
        async with self._sessions() as session, session.begin():
            jobs = await self._store(session).claim(
                batch_size=self._batch_size,
                lease=self._lease,
                organization_id=self._organization_id,
            )
        if not jobs:
            return WorkerStats(enqueued=enqueued)
        stats = replace(await self._process(jobs), enqueued=enqueued, claimed=len(jobs))
        logger.info("embedding.batch", extra={"model": self.model, **asdict(stats)})
        return stats

    async def _process(self, jobs: list[ClaimedJob]) -> WorkerStats:
        async with self._sessions() as session, session.begin():
            store = self._store(session)
            sources = {doc.fact_version_id: doc for doc in await store.documents(jobs)}
            texts = {
                job.job_id: render_fact_document(
                    sources[job.fact_version_id].entity_type,
                    sources[job.fact_version_id].external_id,
                    sources[job.fact_version_id].property,
                    sources[job.fact_version_id].value,
                )
                for job in jobs
            }
            hashes = {job_id: document_hash(text) for job_id, text in texts.items()}
            cache: dict[tuple[UUID, str], list[float]] = {}
            for org_id in {job.organization_id for job in jobs}:
                org_hashes = [hashes[j.job_id] for j in jobs if j.organization_id == org_id]
                for digest, vector in (await store.cached_vectors(org_id, org_hashes)).items():
                    cache[(org_id, digest)] = vector

        # Provider call outside any transaction; each distinct text is sent once.
        to_embed = {
            hashes[j.job_id]: texts[j.job_id]
            for j in jobs
            if (j.organization_id, hashes[j.job_id]) not in cache
        }
        fresh: dict[str, list[float]] = {}
        tokens = 0
        started = time.perf_counter()
        try:
            items = list(to_embed.items())
            for i in range(0, len(items), self._provider.max_batch_size):
                chunk = items[i : i + self._provider.max_batch_size]
                batch = await self._provider.embed([text for _, text in chunk])
                tokens += batch.total_tokens
                fresh.update(zip((digest for digest, _ in chunk), batch.vectors, strict=True))
        except EmbeddingProviderError as exc:
            return await self._record_failure(jobs, f"{type(exc).__name__}: {exc}")
        provider_ms = round((time.perf_counter() - started) * 1000, 2)

        succeeded = reused = lost = 0
        async with self._sessions() as session, session.begin():
            store = self._store(session)
            for job in jobs:
                digest = hashes[job.job_id]
                cached = cache.get((job.organization_id, digest))
                vector = cached if cached is not None else fresh[digest]
                reused += cached is not None
                await store.save_embedding(
                    organization_id=job.organization_id,
                    fact_version_id=job.fact_version_id,
                    vector=vector,
                    text_template=FACT_TEXT_TEMPLATE,
                    content_sha256=digest,
                )
                if await store.complete(job):
                    succeeded += 1
                else:
                    lost += 1
        logger.debug(
            "embedding.provider", extra={"provider_ms": provider_ms, "texts": len(to_embed)}
        )
        return WorkerStats(
            succeeded=succeeded,
            reused=reused,
            embedded=len(to_embed),
            tokens=tokens,
            lease_lost=lost,
        )

    async def _record_failure(self, jobs: list[ClaimedJob], error: str) -> WorkerStats:
        failed = 0
        async with self._sessions() as session, session.begin():
            store = self._store(session)
            for job in jobs:
                final = job.attempts >= self._max_attempts
                failed += final
                await store.fail(
                    job,
                    error=error,
                    retry_in=None
                    if final
                    else retry_delay(
                        job.attempts, base_seconds=self._retry_base, cap_seconds=self._retry_cap
                    ),
                )
        logger.warning(
            "embedding.provider_failed",
            extra={"model": self.model, "jobs": len(jobs), "permanently_failed": failed},
        )
        return WorkerStats(failed=failed)

    async def run_forever(
        self,
        *,
        poll_interval: float,
        stop: asyncio.Event,
        heartbeat: Path | None = None,
        error_backoff: float = 30.0,
    ) -> None:
        """Loop until ``stop`` is set. Unexpected errors (e.g. the database restarting)
        are logged and retried after ``error_backoff`` instead of killing the process.
        """
        while not stop.is_set():
            try:
                stats = await self.run_once()
            except Exception:
                logger.exception("embedding.iteration_failed")
                await _sleep_unless_stopped(stop, error_backoff)
                continue
            if heartbeat is not None:
                await asyncio.to_thread(heartbeat.touch)
            if stats.claimed == 0:
                await _sleep_unless_stopped(stop, poll_interval)


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _main(settings: Settings, *, once: bool, heartbeat: Path | None = None) -> None:
    configure_logging(settings)
    engine = create_engine(settings)
    http_client = (
        build_openai_http_client(settings) if settings.embedding_provider == "openai" else None
    )
    try:
        worker = EmbeddingWorker(
            create_session_factory(engine),
            build_embedding_provider(settings, http_client),
            batch_size=settings.embedding_batch_size,
            max_attempts=settings.embedding_max_attempts,
            lease=timedelta(seconds=settings.embedding_lease_seconds),
            retry_base_seconds=settings.embedding_retry_base_seconds,
            retry_cap_seconds=settings.embedding_retry_cap_seconds,
        )
        logger.info("embedding.worker_started", extra={"model": worker.model, "once": once})
        if once:
            await worker.run_once()
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        if heartbeat is not None:
            await asyncio.to_thread(heartbeat.touch)
        await worker.run_forever(
            poll_interval=settings.embedding_poll_interval_seconds, stop=stop, heartbeat=heartbeat
        )
        logger.info("embedding.worker_stopped")
    finally:
        if http_client is not None:
            await http_client.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextLedger embedding worker")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=None,
        help="touch this file after every healthy iteration (container health checks)",
    )
    args = parser.parse_args()
    asyncio.run(_main(get_settings(), once=args.once, heartbeat=args.heartbeat_file))


if __name__ == "__main__":
    main()
