"""Sources, evidence and provenance of fact versions.

* ``capture_evidence``  store material from a source (idempotent by content hash)
* ``attach``            link evidence to a fact version (append-only)
* ``provenance``        a fact version with its source and all linked evidence
* ``list_sources`` / ``get_source``

Evidence can also be linked atomically while recording a version
(``RecordFactVersion.evidence_ids``); see ``FactService.record_version``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError
from app.domain.evidence import (
    EvidenceRelation,
    EvidenceType,
    content_hash,
    normalize_excerpt,
    validate_metadata,
)
from app.domain.facts import PrivacyScope, require_aware
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.domain.validation import normalize_uri
from app.models.evidence import Evidence
from app.models.source import FactSource
from app.repositories.evidence import EvidenceRepository
from app.repositories.facts import FactRepository
from app.services.authorization import require_permission
from app.services.temporal import to_snapshot
from app.temporal.model import VersionSnapshot


@dataclass(frozen=True, slots=True)
class CapturedEvidence:
    evidence: Evidence
    created: bool  # False when identical content from this source already existed


@dataclass(frozen=True, slots=True)
class LinkedEvidence:
    evidence: Evidence
    source: FactSource
    relation: EvidenceRelation
    linked_at: datetime
    linked_by_user_id: UUID | None


@dataclass(frozen=True, slots=True)
class VersionProvenance:
    """Where one fact version came from: its asserting source and every linked evidence."""

    version: VersionSnapshot
    source: FactSource
    evidence: tuple[LinkedEvidence, ...]


class EvidenceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def capture_evidence(
        self,
        ctx: TenantContext,
        *,
        source_id: UUID,
        evidence_type: EvidenceType,
        excerpt: str,
        captured_at: datetime,
        uri: str | None = None,
        metadata: dict[str, Any] | None = None,
        privacy_scope: PrivacyScope = PrivacyScope.INTERNAL,
    ) -> CapturedEvidence:
        normalized = normalize_excerpt(excerpt)
        digest = content_hash(normalized)
        clean_metadata = validate_metadata(metadata)
        clean_uri = normalize_uri(uri)
        require_aware(captured_at, field="captured_at")

        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            if (
                await FactRepository(self._session, ctx.organization_id).get_source(source_id)
                is None
            ):
                raise NotFoundError("source not found")
            evidence, created = await EvidenceRepository(
                self._session, ctx.organization_id
            ).capture(
                source_id=source_id,
                evidence_type=EvidenceType(evidence_type),
                excerpt=normalized,
                content_sha256=digest,
                uri=clean_uri,
                metadata=clean_metadata,
                privacy_scope=PrivacyScope(privacy_scope),
                captured_at=captured_at,
            )
        return CapturedEvidence(evidence=evidence, created=created)

    async def attach(
        self,
        ctx: TenantContext,
        *,
        fact_version_id: UUID,
        evidence_id: UUID,
        relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
    ) -> bool:
        """Link evidence to a version. Idempotent; returns True if a new link was created."""
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.WRITE_FACTS)
            return (
                await link_evidence(
                    self._session,
                    ctx,
                    fact_version_id=fact_version_id,
                    evidence_ids=[evidence_id],
                    relation=EvidenceRelation(relation),
                )
                > 0
            )

    async def provenance(self, ctx: TenantContext, fact_version_id: UUID) -> VersionProvenance:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            facts = FactRepository(self._session, ctx.organization_id)
            version = await facts.get_version(fact_version_id)
            if version is None:
                raise NotFoundError("fact version not found")
            source = await facts.get_source(version.source_id)
            if source is None:  # pragma: no cover - guaranteed by a foreign key
                raise NotFoundError("source not found")
            rows = await EvidenceRepository(
                self._session, ctx.organization_id
            ).evidence_for_version(fact_version_id)

        return VersionProvenance(
            version=to_snapshot(version),
            source=source,
            evidence=tuple(
                LinkedEvidence(
                    evidence=evidence,
                    source=evidence_source,
                    relation=link.relation,
                    linked_at=link.linked_at,
                    linked_by_user_id=link.linked_by_user_id,
                )
                for link, evidence, evidence_source in rows
            ),
        )

    async def list_sources(self, ctx: TenantContext) -> Sequence[FactSource]:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            return await EvidenceRepository(self._session, ctx.organization_id).list_sources()

    async def get_source(self, ctx: TenantContext, source_id: UUID) -> FactSource:
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            source = await FactRepository(self._session, ctx.organization_id).get_source(source_id)
            if source is None:
                raise NotFoundError("source not found")
            return source


async def link_evidence(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    fact_version_id: UUID,
    evidence_ids: Sequence[UUID],
    relation: EvidenceRelation,
) -> int:
    """Link evidence to a version inside the caller's transaction. Returns links created.

    All ids must belong to the caller's organization; otherwise NotFound is raised
    and the caller's transaction (e.g. the version insert) is rolled back.
    A link that already exists with a *different* relation is a conflict: the
    history of what evidence meant for a version is never rewritten.
    """
    unique_ids = list(dict.fromkeys(evidence_ids))
    if not unique_ids:
        return 0
    if await FactRepository(session, ctx.organization_id).get_version(fact_version_id) is None:
        raise NotFoundError("fact version not found")
    repository = EvidenceRepository(session, ctx.organization_id)
    found = {e.id for e in await repository.get_many(unique_ids)}
    if missing := [str(i) for i in unique_ids if i not in found]:
        raise NotFoundError(f"evidence not found: {', '.join(missing)}")

    created = 0
    for evidence_id in unique_ids:
        existing = await repository.get_link(fact_version_id, evidence_id)
        if existing is not None:
            if existing.relation is not relation:
                raise ConflictError(
                    f"evidence {evidence_id} is already linked as {existing.relation}"
                )
            continue
        if await repository.link(
            fact_version_id=fact_version_id,
            evidence_id=evidence_id,
            relation=relation,
            linked_by_user_id=ctx.user_id,
        ):
            created += 1
    return created
