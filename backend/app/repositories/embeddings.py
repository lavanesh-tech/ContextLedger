"""Data access for embeddings and embedding jobs.

* ``EmbeddingRepository`` is tenant-bound, like every read path.
* ``EmbeddingJobStore`` belongs to the background worker, which serves all
  tenants. Every row it writes carries the job's own organization_id, it never
  copies data between organizations, and it is never reachable from the API.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.embeddings import EMBEDDING_DIMENSIONS, EmbeddingJobStatus
from app.models.embedding import EmbeddingJob, FactEmbedding
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion


class EmbeddingRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    async def get(self, fact_version_id: UUID, model: str) -> FactEmbedding | None:
        result = await self._session.scalars(
            select(FactEmbedding).where(
                FactEmbedding.organization_id == self.organization_id,
                FactEmbedding.fact_version_id == fact_version_id,
                FactEmbedding.model == model,
            )
        )
        return result.one_or_none()

    async def nearest(
        self, *, model: str, query: Sequence[float], limit: int
    ) -> Sequence[tuple[UUID, float]]:
        """(fact_version_id, cosine distance) of the closest embeddings in this tenant."""
        distance = FactEmbedding.embedding.cosine_distance(list(query))
        statement = (
            select(FactEmbedding.fact_version_id, distance.label("distance"))
            .where(
                FactEmbedding.organization_id == self.organization_id,
                FactEmbedding.model == model,
            )
            .order_by(distance)
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [(version_id, float(d)) for version_id, d in result.tuples()]


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: UUID
    organization_id: UUID
    fact_version_id: UUID
    lease_token: UUID
    attempts: int


@dataclass(frozen=True, slots=True)
class FactDocumentSource:
    organization_id: UUID
    fact_version_id: UUID
    entity_type: str
    external_id: str
    property: str
    value: Any


class EmbeddingJobStore:
    def __init__(self, session: AsyncSession, *, model: str) -> None:
        self._session = session
        self._model = model

    async def enqueue_missing(self, *, limit: int, organization_id: UUID | None = None) -> int:
        """Create PENDING jobs for fact versions with neither an embedding nor a job.

        A reconciliation sweep: no version can be forgotten, even if an event
        was lost. (Kafka-triggered enqueueing arrives in Phase 15.)
        """
        has_job = select(EmbeddingJob.id).where(
            EmbeddingJob.fact_version_id == FactVersion.id, EmbeddingJob.model == self._model
        )
        has_embedding = select(FactEmbedding.fact_version_id).where(
            FactEmbedding.fact_version_id == FactVersion.id, FactEmbedding.model == self._model
        )
        candidates = (
            select(FactVersion.organization_id, FactVersion.id)
            .where(~has_job.exists(), ~has_embedding.exists())
            .order_by(FactVersion.recorded_at)
            .limit(limit)
        )
        if organization_id is not None:
            candidates = candidates.where(FactVersion.organization_id == organization_id)
        rows = (await self._session.execute(candidates)).tuples().all()
        if not rows:
            return 0
        result = await self._session.execute(
            insert(EmbeddingJob)
            .values(
                [
                    {
                        "organization_id": org_id,
                        "fact_version_id": version_id,
                        "model": self._model,
                        "status": EmbeddingJobStatus.PENDING,
                    }
                    for org_id, version_id in rows
                ]
            )
            .on_conflict_do_nothing(index_elements=["fact_version_id", "model"])
            .returning(EmbeddingJob.id)
        )
        return len(result.all())

    async def claim(
        self, *, batch_size: int, lease: timedelta, organization_id: UUID | None = None
    ) -> list[ClaimedJob]:
        """Lease up to ``batch_size`` due jobs. Concurrent workers never get the same job."""
        now = func.now()
        due = and_(
            EmbeddingJob.model == self._model,
            EmbeddingJob.available_at <= now,
            or_(
                EmbeddingJob.status == EmbeddingJobStatus.PENDING,
                and_(
                    EmbeddingJob.status == EmbeddingJobStatus.RUNNING,
                    EmbeddingJob.locked_at < now - lease,  # the previous worker died
                ),
            ),
        )
        statement = (
            select(EmbeddingJob)
            .where(due)
            .order_by(EmbeddingJob.available_at, EmbeddingJob.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        if organization_id is not None:
            statement = statement.where(EmbeddingJob.organization_id == organization_id)
        jobs = (await self._session.scalars(statement)).all()

        claimed: list[ClaimedJob] = []
        locked_at = await self._session.scalar(select(func.now()))
        for job in jobs:
            token = uuid4()
            job.status = EmbeddingJobStatus.RUNNING
            job.lease_token = token
            job.locked_at = locked_at
            job.attempts += 1
            claimed.append(
                ClaimedJob(
                    job_id=job.id,
                    organization_id=job.organization_id,
                    fact_version_id=job.fact_version_id,
                    lease_token=token,
                    attempts=job.attempts,
                )
            )
        await self._session.flush()
        return claimed

    async def documents(self, jobs: Sequence[ClaimedJob]) -> list[FactDocumentSource]:
        ids = [job.fact_version_id for job in jobs]
        statement = (
            select(
                FactVersion.organization_id,
                FactVersion.id,
                Entity.entity_type,
                Entity.external_id,
                Fact.property,
                FactVersion.value,
            )
            .join(
                Fact,
                and_(
                    Fact.organization_id == FactVersion.organization_id,
                    Fact.id == FactVersion.fact_id,
                ),
            )
            .join(
                Entity,
                and_(Entity.organization_id == Fact.organization_id, Entity.id == Fact.entity_id),
            )
            .where(FactVersion.id.in_(ids))
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return [FactDocumentSource(*row) for row in rows]

    async def cached_vectors(
        self, organization_id: UUID, hashes: Sequence[str]
    ) -> dict[str, list[float]]:
        """Existing vectors for identical text in the *same* organization (never across tenants)."""
        if not hashes:
            return {}
        statement = (
            select(FactEmbedding.content_sha256, FactEmbedding.embedding)
            .where(
                FactEmbedding.organization_id == organization_id,
                FactEmbedding.model == self._model,
                FactEmbedding.content_sha256.in_(list(hashes)),
            )
            .distinct(FactEmbedding.content_sha256)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return {digest: list(vector) for digest, vector in rows}

    async def save_embedding(
        self,
        *,
        organization_id: UUID,
        fact_version_id: UUID,
        vector: Sequence[float],
        text_template: str,
        content_sha256: str,
    ) -> None:
        await self._session.execute(
            insert(FactEmbedding)
            .values(
                organization_id=organization_id,
                fact_version_id=fact_version_id,
                model=self._model,
                dimensions=EMBEDDING_DIMENSIONS,
                embedding=list(vector),
                text_template=text_template,
                content_sha256=content_sha256,
            )
            .on_conflict_do_nothing(index_elements=["fact_version_id", "model"])
        )

    async def complete(self, job: ClaimedJob) -> bool:
        """Mark SUCCEEDED if we still hold the lease. Returns False if the lease was lost."""
        result = await self._session.execute(
            update(EmbeddingJob)
            .where(EmbeddingJob.id == job.job_id, EmbeddingJob.lease_token == job.lease_token)
            .values(
                status=EmbeddingJobStatus.SUCCEEDED,
                lease_token=None,
                locked_at=None,
                last_error=None,
            )
            .returning(EmbeddingJob.id)
        )
        return result.first() is not None

    async def fail(self, job: ClaimedJob, *, error: str, retry_in: timedelta | None) -> None:
        """Back to PENDING after ``retry_in``, or FAILED for good when ``retry_in`` is None."""
        values: dict[str, Any] = {
            "lease_token": None,
            "locked_at": None,
            "last_error": error[:500],
        }
        if retry_in is None:
            values["status"] = EmbeddingJobStatus.FAILED
        else:
            values["status"] = EmbeddingJobStatus.PENDING
            values["available_at"] = func.now() + retry_in
        await self._session.execute(
            update(EmbeddingJob)
            .where(EmbeddingJob.id == job.job_id, EmbeddingJob.lease_token == job.lease_token)
            .values(**values)
        )
