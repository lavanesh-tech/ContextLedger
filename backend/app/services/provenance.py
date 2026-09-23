"""Provenance graph queries: "what does this depend on?" and "what depends on this?".

Authorization and tenancy are checked in PostgreSQL (``decisions:read``), then
the traversal runs in Neo4j scoped to the caller's organization. Every answer
reports ``pending_events``: how many changes for this organization are still in
the outbox. The graph is eventually consistent with PostgreSQL, and callers
should be able to see that.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import NotFoundError
from app.domain.retrieval import visible_privacy_scopes
from app.domain.roles import MembershipRole, Permission
from app.domain.tenancy import TenantContext
from app.provenance.graph import DecisionRef, GraphReader, ImpactRow
from app.repositories.graph_outbox import GraphOutboxRepository
from app.services.authorization import require_permission


@dataclass(frozen=True, slots=True)
class AffectedDecision:
    decision_id: UUID
    action: str
    decided_at: datetime | None
    # (fact version, how it is connected) pairs explaining why the decision is affected
    because_of: tuple[tuple[UUID, str], ...]


@dataclass(frozen=True, slots=True)
class ImpactReport:
    subject_id: UUID
    affected_versions: tuple[UUID, ...]
    decisions: tuple[AffectedDecision, ...]
    superseded_by: tuple[UUID, ...]  # only for fact versions
    pending_events: int


@dataclass(frozen=True, slots=True)
class LineageEvidence:
    evidence_id: UUID
    relation: str
    source_id: UUID | None


@dataclass(frozen=True, slots=True)
class LineageStep:
    fact_version_id: UUID
    privacy_scope: str
    redacted: bool  # above the reader's privacy ceiling: only id and scope are shown
    entity_type: str | None
    external_id: str | None
    property: str | None
    source_id: UUID | None
    source_name: str | None
    evidence: tuple[LineageEvidence, ...]


@dataclass(frozen=True, slots=True)
class DecisionLineage:
    decision_id: UUID
    relied_on: tuple[LineageStep, ...]
    pending_events: int


def group_impact(rows: list[ImpactRow]) -> tuple[tuple[UUID, ...], tuple[AffectedDecision, ...]]:
    """Collapse (version, via, decision) rows into versions and decisions, deterministically."""
    versions = sorted({row.fact_version_id for row in rows})
    reasons: dict[UUID, set[tuple[UUID, str]]] = defaultdict(set)
    refs: dict[UUID, DecisionRef] = {}
    for row in rows:
        if row.decision is not None:
            refs[row.decision.decision_id] = row.decision
            reasons[row.decision.decision_id].add((row.fact_version_id, row.via))
    decisions = sorted(
        (
            AffectedDecision(
                decision_id=ref.decision_id,
                action=ref.action,
                decided_at=ref.decided_at,
                because_of=tuple(sorted(reasons[ref.decision_id])),
            )
            for ref in refs.values()
        ),
        key=lambda d: (d.decided_at is None, d.decided_at, d.decision_id),
    )
    return tuple(versions), tuple(decisions)


class ProvenanceService:
    def __init__(self, session: AsyncSession, graph: GraphReader) -> None:
        self._session = session
        self._graph = graph

    async def _authorize(self, ctx: TenantContext) -> tuple[MembershipRole, int]:
        async with self._session.begin():
            role = await require_permission(self._session, ctx, Permission.READ_DECISIONS)
            pending = await GraphOutboxRepository(self._session).pending(ctx.organization_id)
            return role, pending

    async def _require(self, ctx: TenantContext, label: str, node_id: UUID) -> None:
        if not await self._graph.exists(label, org=ctx.organization_id, node_id=node_id):
            raise NotFoundError(f"{label} not found in the provenance graph")

    async def impact_of_fact_version(self, ctx: TenantContext, version_id: UUID) -> ImpactReport:
        """Decisions that relied on (or had in context) this version, and its successors."""
        _, pending = await self._authorize(ctx)
        await self._require(ctx, "FactVersion", version_id)
        rows = await self._graph.impact_of_version(org=ctx.organization_id, version_id=version_id)
        _, decisions = group_impact(rows)
        later = await self._graph.superseded_by(org=ctx.organization_id, version_id=version_id)
        return ImpactReport(
            subject_id=version_id,
            affected_versions=(version_id,),
            decisions=decisions,
            superseded_by=tuple(later),
            pending_events=pending,
        )

    async def impact_of_source(self, ctx: TenantContext, source_id: UUID) -> ImpactReport:
        """If this source were wrong: which versions and decisions depend on it?"""
        _, pending = await self._authorize(ctx)
        await self._require(ctx, "Source", source_id)
        rows = await self._graph.impact_of_source(org=ctx.organization_id, source_id=source_id)
        versions, decisions = group_impact(rows)
        return ImpactReport(
            subject_id=source_id,
            affected_versions=versions,
            decisions=decisions,
            superseded_by=(),
            pending_events=pending,
        )

    async def impact_of_evidence(self, ctx: TenantContext, evidence_id: UUID) -> ImpactReport:
        _, pending = await self._authorize(ctx)
        await self._require(ctx, "Evidence", evidence_id)
        rows = await self._graph.impact_of_evidence(
            org=ctx.organization_id, evidence_id=evidence_id
        )
        versions, decisions = group_impact(rows)
        return ImpactReport(
            subject_id=evidence_id,
            affected_versions=versions,
            decisions=decisions,
            superseded_by=(),
            pending_events=pending,
        )

    async def decision_lineage(self, ctx: TenantContext, decision_id: UUID) -> DecisionLineage:
        """Upstream provenance of a decision: versions → entity, source, evidence."""
        role, pending = await self._authorize(ctx)
        visible = {str(scope) for scope in visible_privacy_scopes(role)}
        await self._require(ctx, "Decision", decision_id)
        records = await self._graph.decision_lineage(
            org=ctx.organization_id, decision_id=decision_id
        )
        return DecisionLineage(
            decision_id=decision_id,
            relied_on=tuple(_lineage_step(r, visible) for r in records),
            pending_events=pending,
        )


def _lineage_step(record: dict[str, Any], visible: set[str]) -> LineageStep:
    version_id = UUID(record["version_id"])
    scope = str(record["privacy_scope"])
    if scope not in visible:
        return LineageStep(
            fact_version_id=version_id,
            privacy_scope=scope,
            redacted=True,
            entity_type=None,
            external_id=None,
            property=None,
            source_id=None,
            source_name=None,
            evidence=(),
        )
    evidence = sorted(
        (
            LineageEvidence(
                evidence_id=UUID(e["id"]),
                relation=e["relation"],
                source_id=None if e["source_id"] is None else UUID(e["source_id"]),
            )
            for e in record["evidence"]
            if e is not None
        ),
        key=lambda e: e.evidence_id,
    )
    return LineageStep(
        fact_version_id=version_id,
        privacy_scope=scope,
        redacted=False,
        entity_type=record["entity_type"],
        external_id=record["external_id"],
        property=record["property"],
        source_id=UUID(record["source_id"]),
        source_name=record["source_name"],
        evidence=tuple(evidence),
    )
