"""Hybrid temporal retrieval: "which facts are relevant to this question, as of T / K?"

Pipeline (see docs/RETRIEVAL.md):

1. validate the request (pure, no I/O);
2. check ``facts:read`` against the actor's current membership;
3. embed the question, *outside* any transaction (a slow provider never holds
   a database connection); if the provider fails, continue with full-text only
   and say so in the result (``vector_search="unavailable"``);
4. in one REPEATABLE READ, read-only transaction: re-check the permission,
   derive the visible privacy scopes from the *current* role, fetch both
   candidate lists in one statement, fuse and rank them (app/domain/retrieval.py),
   and load the winners;
5. return each fact exactly as it looked at ``known_at``, with its source and a
   per-result score breakdown, so callers can see *why* it ranked where it did.

Tenant, time, privacy and ownership are decided here by deterministic code.
No model decides any of them.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ValidationFailedError
from app.domain.facts import PrivacyScope, SourceType, require_aware
from app.domain.retrieval import (
    DEFAULT_TRUST_WEIGHT,
    RankedCandidate,
    candidate_pool_size,
    fuse,
    normalize_query,
    trust_score,
    validate_limit,
    validate_trust_weight,
    visible_privacy_scopes,
)
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.domain.validation import normalize_external_id, normalize_identifier
from app.providers.embeddings import EmbeddingProvider, EmbeddingProviderError
from app.repositories.retrieval import RetrievalFilters, RetrievalRepository
from app.services.authorization import require_permission
from app.services.temporal import to_snapshot
from app.temporal import reference
from app.temporal.model import VersionSnapshot

logger = logging.getLogger("contextledger.retrieval")

VectorSearchStatus = Literal["used", "unavailable"]
MAX_FILTER_VALUES = 100


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query: str
    limit: int = 10
    valid_at: datetime | None = None  # default: now
    known_at: datetime | None = None  # default: everything recorded so far
    entity_type: str | None = None
    external_ids: Sequence[str] = ()
    properties: Sequence[str] = ()
    source_ids: Sequence[UUID] = ()
    min_authority: int | None = None
    min_confidence: Decimal | float | str | None = None
    max_privacy_scope: PrivacyScope | None = None  # can only narrow the role's ceiling
    trust_weight: float = DEFAULT_TRUST_WEIGHT


@dataclass(frozen=True, slots=True)
class RetrievedFact:
    version: VersionSnapshot  # as it looked at known_at
    entity_type: str
    external_id: str
    property: str
    source_name: str
    source_type: SourceType
    ranking: RankedCandidate


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: str
    valid_at: datetime
    known_at: datetime | None
    vector_search: VectorSearchStatus
    embedding_model: str
    privacy_scopes: tuple[PrivacyScope, ...]
    vector_candidates: int
    text_candidates: int
    results: list[RetrievedFact] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_filters(request: RetrievalQuery) -> RetrievalFilters:
    for name in ("external_ids", "properties", "source_ids"):
        if len(getattr(request, name)) > MAX_FILTER_VALUES:
            raise ValidationFailedError(f"{name} accepts at most {MAX_FILTER_VALUES} values")
    if request.min_authority is not None and not 0 <= request.min_authority <= 100:
        raise ValidationFailedError("min_authority must be between 0 and 100")
    min_confidence = None
    if request.min_confidence is not None:
        min_confidence = Decimal(str(request.min_confidence))
        if not Decimal(0) <= min_confidence <= Decimal(1):
            raise ValidationFailedError("min_confidence must be between 0 and 1")
    return RetrievalFilters(
        entity_type=(
            normalize_identifier(request.entity_type, field="entity_type")
            if request.entity_type is not None
            else None
        ),
        external_ids=tuple(sorted({normalize_external_id(x) for x in request.external_ids})),
        properties=tuple(
            sorted({normalize_identifier(p, field="property") for p in request.properties})
        ),
        source_ids=tuple(sorted(set(request.source_ids))),
        min_authority=request.min_authority,
        min_confidence=min_confidence,
    )


class RetrievalService:
    def __init__(
        self,
        session: AsyncSession,
        provider: EmbeddingProvider,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session = session
        self._provider = provider
        self._clock = clock

    async def search(self, ctx: TenantContext, request: RetrievalQuery) -> RetrievalResult:
        query = normalize_query(request.query)
        limit = validate_limit(request.limit)
        trust_weight = validate_trust_weight(request.trust_weight)
        filters = _normalize_filters(request)
        valid_at = require_aware(request.valid_at or self._clock(), field="valid_at")
        known_at = (
            require_aware(request.known_at, field="known_at")
            if request.known_at is not None
            else None
        )

        # Fail fast before paying for an embedding.
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)

        query_vector = await self._embed(query)
        pool = candidate_pool_size(limit)

        async with self._session.begin():
            await self._session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            await self._session.execute(text("SET TRANSACTION READ ONLY"))
            role = await require_permission(self._session, ctx, Permission.READ_FACTS)
            scopes = visible_privacy_scopes(role, request.max_privacy_scope, ctx.max_privacy_scope)
            # pgvector >= 0.8: keep scanning the HNSW graph until enough rows pass
            # the WHERE filters, instead of returning too few filtered results.
            await self._session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            await self._session.execute(text(f"SET LOCAL hnsw.ef_search = {max(40, pool)}"))

            repository = RetrievalRepository(self._session, ctx.organization_id)
            vector_hits, text_hits = await repository.candidates(
                query=query,
                query_vector=query_vector,
                model=self._provider.model_id,
                valid_at=valid_at,
                known_at=known_at,
                scopes=scopes,
                filters=filters,
                pool=pool,
            )
            ids = {h.fact_version_id for h in vector_hits} | {h.fact_version_id for h in text_hits}
            rows = await repository.hydrate(sorted(ids))

        ranked = fuse(
            vector_hits,
            text_hits,
            {
                version_id: trust_score(row.version.authority, row.version.confidence)
                for version_id, row in rows.items()
            },
            trust_weight=trust_weight,
        )
        results = [
            RetrievedFact(
                version=reference.as_known(to_snapshot(rows[c.fact_version_id].version), known_at),
                entity_type=rows[c.fact_version_id].entity_type,
                external_id=rows[c.fact_version_id].external_id,
                property=rows[c.fact_version_id].property,
                source_name=rows[c.fact_version_id].source_name,
                source_type=rows[c.fact_version_id].source_type,
                ranking=c,
            )
            for c in ranked[:limit]
        ]
        logger.info(
            "retrieval.search",
            extra={
                "organization_id": str(ctx.organization_id),
                "vector_candidates": len(vector_hits),
                "text_candidates": len(text_hits),
                "returned": len(results),
                "vector_search": "used" if query_vector is not None else "unavailable",
            },
        )
        return RetrievalResult(
            query=query,
            valid_at=valid_at,
            known_at=known_at,
            vector_search="used" if query_vector is not None else "unavailable",
            embedding_model=self._provider.model_id,
            privacy_scopes=tuple(s for s in PrivacyScope if s in scopes),
            vector_candidates=len(vector_hits),
            text_candidates=len(text_hits),
            results=results,
        )

    async def _embed(self, query: str) -> list[float] | None:
        try:
            batch = await self._provider.embed([query])
        except EmbeddingProviderError as exc:
            logger.warning(
                "retrieval.embedding_unavailable",
                extra={"model": self._provider.model_id, "error": str(exc)[:200]},
            )
            return None
        return batch.vectors[0]
