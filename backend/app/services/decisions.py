"""Decision receipts: freeze the context, record the decision, prove it later.

1. ``capture_context`` runs hybrid retrieval with ``known_at`` pinned to the
   database clock (unless the caller pins it), and stores the ranked result as an
   immutable **context snapshot**. Re-reading the snapshot later shows the facts
   exactly as they were known then, even after they were superseded or corrected.
2. ``record_decision`` stores what was decided on the basis of one snapshot and
   which of its facts it relied on, sealed with a SHA-256 receipt hash.
3. ``receipt`` returns the full receipt: decision, context, every fact (as known
   at ``known_at``) with its source and the evidence linked by then, and whether
   the stored hash still matches the stored rows.

Permissions: capturing and recording need ``decisions:record``; reading a
receipt needs ``decisions:read``. Facts above the reader's privacy ceiling are
redacted in the receipt (``redacted=True``), but they still count towards the
hash, so integrity can be verified by anyone allowed to read the receipt.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.decisions import (
    ReceiptContent,
    normalize_action,
    normalize_agent,
    normalize_rationale,
    receipt_hash,
    validate_outcome,
    validate_relied_on,
    value_sha256,
)
from app.domain.errors import NotFoundError
from app.domain.evidence import EvidenceRelation
from app.domain.facts import PrivacyScope, require_aware
from app.domain.retrieval import visible_privacy_scopes
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.models.decision import ContextSnapshot, ContextSnapshotFact, Decision, DecisionFact
from app.providers.embeddings import EmbeddingProvider
from app.repositories.decisions import DecisionRepository, SnapshotFactRow
from app.repositories.evidence import EvidenceRepository
from app.services.authorization import require_permission
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService
from app.services.temporal import to_snapshot
from app.temporal import reference
from app.temporal.model import VersionSnapshot


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CapturedContext:
    snapshot_id: UUID
    valid_at: datetime
    known_at: datetime
    retrieval: RetrievalResult


@dataclass(frozen=True, slots=True)
class RecordDecision:
    snapshot_id: UUID
    action: str  # e.g. "credit.approve_increase"
    outcome: Any  # JSON, e.g. {"approved": true, "new_limit": 7500}
    relied_on: Sequence[UUID]  # fact versions from the snapshot the decision depended on
    rationale: str | None = None
    agent: str | None = None  # e.g. "credit-review-agent v3"


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    evidence_id: UUID
    relation: EvidenceRelation
    content_sha256: str
    uri: str | None
    source_name: str
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class ReceiptFact:
    fact_version_id: UUID
    position: int
    relied_on: bool
    redacted: bool  # above the reader's privacy ceiling: identity kept, content hidden
    privacy_scope: PrivacyScope
    entity_type: str | None
    external_id: str | None
    property: str | None
    source_name: str | None
    version: VersionSnapshot | None  # as known at the snapshot's known_at
    ranking: dict[str, Any]
    evidence: tuple[ReceiptEvidence, ...]


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    snapshot_id: UUID
    query: str
    valid_at: datetime
    known_at: datetime
    embedding_model: str
    vector_search: str
    privacy_scopes: tuple[str, ...]
    parameters: dict[str, Any]
    captured_by_user_id: UUID


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    decision_id: UUID
    organization_id: UUID
    action: str
    outcome: Any
    rationale: str | None
    agent: str | None
    decided_by_user_id: UUID
    decided_at: datetime
    context: ReceiptContext
    facts: tuple[ReceiptFact, ...]
    receipt_sha256: str
    integrity_verified: bool  # stored hash == hash recomputed from the stored rows


def _content(
    decision: Decision,
    snapshot: ContextSnapshot,
    rows: Sequence[SnapshotFactRow],
    relied_on: frozenset[UUID],
) -> ReceiptContent:
    return ReceiptContent(
        organization_id=decision.organization_id,
        decision_id=decision.id,
        snapshot_id=snapshot.id,
        decided_at=decision.decided_at,
        decided_by_user_id=decision.decided_by_user_id,
        agent=decision.agent,
        action=decision.action,
        outcome=decision.outcome,
        rationale=decision.rationale,
        query=snapshot.query,
        valid_at=snapshot.valid_at,
        known_at=snapshot.known_at,
        embedding_model=snapshot.embedding_model,
        vector_search=snapshot.vector_search,
        privacy_scopes=tuple(snapshot.privacy_scopes),
        facts={
            row.version.id: (row.snapshot_fact.position, value_sha256(row.version.value))
            for row in rows
        },
        relied_on=relied_on,
    )


def _parameters(request: RetrievalQuery) -> dict[str, Any]:
    return {
        "limit": request.limit,
        "entity_type": request.entity_type,
        "external_ids": sorted(request.external_ids),
        "properties": sorted(request.properties),
        "source_ids": sorted(str(s) for s in request.source_ids),
        "min_authority": request.min_authority,
        "min_confidence": None if request.min_confidence is None else str(request.min_confidence),
        "max_privacy_scope": request.max_privacy_scope,
        "trust_weight": request.trust_weight,
    }


class DecisionService:
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

    async def capture_context(self, ctx: TenantContext, request: RetrievalQuery) -> CapturedContext:
        """Retrieve and freeze the context an actor is about to decide with."""
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.RECORD_DECISIONS)
            database_now = await DecisionRepository(
                self._session, ctx.organization_id
            ).database_now()
        known_at = (
            require_aware(request.known_at, field="known_at")
            if request.known_at is not None
            else database_now
        )
        valid_at = (
            require_aware(request.valid_at, field="valid_at")
            if request.valid_at is not None
            else known_at
        )
        pinned = replace(request, valid_at=valid_at, known_at=known_at)

        retrieval = await RetrievalService(self._session, self._provider, clock=self._clock).search(
            ctx, pinned
        )

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.RECORD_DECISIONS)
            snapshot = ContextSnapshot(
                id=uuid4(),
                organization_id=ctx.organization_id,
                captured_by_user_id=ctx.user_id,
                query=retrieval.query,
                valid_at=valid_at,
                known_at=known_at,
                embedding_model=retrieval.embedding_model,
                vector_search=retrieval.vector_search,
                privacy_scopes=[str(s) for s in retrieval.privacy_scopes],
                parameters=_parameters(pinned),
            )
            facts = [
                ContextSnapshotFact(
                    organization_id=ctx.organization_id,
                    snapshot_id=snapshot.id,
                    fact_version_id=result.version.id,
                    position=position,
                    ranking={
                        "score": result.ranking.score,
                        "rrf_score": result.ranking.rrf_score,
                        "trust": result.ranking.trust,
                        "vector_rank": result.ranking.vector_rank,
                        "vector_distance": result.ranking.vector_distance,
                        "text_rank": result.ranking.text_rank,
                        "text_score": result.ranking.text_score,
                    },
                )
                for position, result in enumerate(retrieval.results, start=1)
            ]
            await DecisionRepository(self._session, ctx.organization_id).add_snapshot(
                snapshot, facts
            )
        return CapturedContext(
            snapshot_id=snapshot.id, valid_at=valid_at, known_at=known_at, retrieval=retrieval
        )

    async def record_decision(self, ctx: TenantContext, command: RecordDecision) -> DecisionReceipt:
        action = normalize_action(command.action)
        outcome = validate_outcome(command.outcome)
        rationale = normalize_rationale(command.rationale)
        agent = normalize_agent(command.agent)

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.RECORD_DECISIONS)
            repository = DecisionRepository(self._session, ctx.organization_id)
            snapshot = await repository.get_snapshot(command.snapshot_id)
            if snapshot is None:
                raise NotFoundError("context snapshot not found")
            rows = await repository.snapshot_rows(snapshot.id)
            relied_on = validate_relied_on(command.relied_on, (r.version.id for r in rows))

            decision = Decision(
                id=uuid4(),
                organization_id=ctx.organization_id,
                snapshot_id=snapshot.id,
                decided_by_user_id=ctx.user_id,
                agent=agent,
                action=action,
                outcome=outcome,
                rationale=rationale,
                decided_at=await repository.database_now(),
                receipt_sha256="0" * 64,  # replaced below, once every input is known
            )
            decision.receipt_sha256 = receipt_hash(
                _content(decision, snapshot, rows, frozenset(relied_on))
            )
            await repository.add_decision(
                decision,
                [
                    DecisionFact(
                        organization_id=ctx.organization_id,
                        decision_id=decision.id,
                        snapshot_id=snapshot.id,
                        fact_version_id=version_id,
                    )
                    for version_id in relied_on
                ],
            )
        return await self.receipt(ctx, decision.id)

    async def receipt(self, ctx: TenantContext, decision_id: UUID) -> DecisionReceipt:
        async with self._session.begin():
            role = await require_permission(self._session, ctx, Permission.READ_DECISIONS)
            visible = visible_privacy_scopes(role)
            repository = DecisionRepository(self._session, ctx.organization_id)
            decision = await repository.get_decision(decision_id)
            if decision is None:
                raise NotFoundError("decision not found")
            snapshot = await repository.get_snapshot(decision.snapshot_id)
            if snapshot is None:  # pragma: no cover - guaranteed by a foreign key
                raise NotFoundError("context snapshot not found")
            rows = await repository.snapshot_rows(snapshot.id)
            relied_on = await repository.relied_on(decision.id)
            evidence_repository = EvidenceRepository(self._session, ctx.organization_id)
            facts = []
            for row in rows:
                scope = PrivacyScope(row.version.privacy_scope)
                if scope not in visible:
                    facts.append(self._redacted(row, scope, relied_on))
                    continue
                links = await evidence_repository.evidence_for_version(row.version.id)
                facts.append(
                    ReceiptFact(
                        fact_version_id=row.version.id,
                        position=row.snapshot_fact.position,
                        relied_on=row.version.id in relied_on,
                        redacted=False,
                        privacy_scope=scope,
                        entity_type=row.entity_type,
                        external_id=row.external_id,
                        property=row.property,
                        source_name=row.source_name,
                        version=reference.as_known(to_snapshot(row.version), snapshot.known_at),
                        ranking=dict(row.snapshot_fact.ranking),
                        evidence=tuple(
                            ReceiptEvidence(
                                evidence_id=evidence.id,
                                relation=link.relation,
                                content_sha256=evidence.content_sha256,
                                uri=evidence.uri,
                                source_name=source.name,
                                linked_at=link.linked_at,
                            )
                            for link, evidence, source in links
                            # Only evidence the actor could have seen when deciding.
                            if link.linked_at <= snapshot.known_at
                            and PrivacyScope(evidence.privacy_scope) in visible
                        ),
                    )
                )
            recomputed = receipt_hash(_content(decision, snapshot, rows, relied_on))

        return DecisionReceipt(
            decision_id=decision.id,
            organization_id=decision.organization_id,
            action=decision.action,
            outcome=decision.outcome,
            rationale=decision.rationale,
            agent=decision.agent,
            decided_by_user_id=decision.decided_by_user_id,
            decided_at=decision.decided_at,
            context=ReceiptContext(
                snapshot_id=snapshot.id,
                query=snapshot.query,
                valid_at=snapshot.valid_at,
                known_at=snapshot.known_at,
                embedding_model=snapshot.embedding_model,
                vector_search=snapshot.vector_search,
                privacy_scopes=tuple(snapshot.privacy_scopes),
                parameters=dict(snapshot.parameters),
                captured_by_user_id=snapshot.captured_by_user_id,
            ),
            facts=tuple(facts),
            receipt_sha256=decision.receipt_sha256,
            integrity_verified=recomputed == decision.receipt_sha256,
        )

    async def decisions_relying_on(self, ctx: TenantContext, fact_version_id: UUID) -> list[UUID]:
        """Ids of decisions that relied on a fact version, oldest first."""
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_DECISIONS)
            decisions = await DecisionRepository(
                self._session, ctx.organization_id
            ).decisions_relying_on(fact_version_id)
            return [d.id for d in decisions]

    @staticmethod
    def _redacted(
        row: SnapshotFactRow, scope: PrivacyScope, relied_on: frozenset[UUID]
    ) -> ReceiptFact:
        return ReceiptFact(
            fact_version_id=row.version.id,
            position=row.snapshot_fact.position,
            relied_on=row.version.id in relied_on,
            redacted=True,
            privacy_scope=scope,
            entity_type=None,
            external_id=None,
            property=None,
            source_name=None,
            version=None,
            ranking={},
            evidence=(),
        )
