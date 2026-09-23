"""Tenant-bound data access for evidence and evidence links. Never commits."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.evidence import EvidenceRelation, EvidenceType
from app.domain.facts import PrivacyScope
from app.models.evidence import Evidence, FactVersionEvidence
from app.models.source import FactSource


class EvidenceRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    async def capture(
        self,
        *,
        source_id: UUID,
        evidence_type: EvidenceType,
        excerpt: str,
        content_sha256: str,
        uri: str | None,
        metadata: dict[str, Any],
        privacy_scope: PrivacyScope,
        captured_at: datetime,
    ) -> tuple[Evidence, bool]:
        """Insert evidence, or return the existing row with the same content hash.

        Returns (evidence, created). Race-safe via ON CONFLICT DO NOTHING on
        (organization_id, source_id, content_sha256).
        """
        inserted_id = await self._session.scalar(
            insert(Evidence)
            .values(
                organization_id=self.organization_id,
                source_id=source_id,
                evidence_type=evidence_type,
                excerpt=excerpt,
                content_sha256=content_sha256,
                uri=uri,
                metadata_=metadata,
                privacy_scope=privacy_scope,
                captured_at=captured_at,
            )
            .on_conflict_do_nothing(
                index_elements=["organization_id", "source_id", "content_sha256"]
            )
            .returning(Evidence.id)
        )
        result = await self._session.scalars(
            select(Evidence).where(
                Evidence.organization_id == self.organization_id,
                Evidence.source_id == source_id,
                Evidence.content_sha256 == content_sha256,
            )
        )
        return result.one(), inserted_id is not None

    async def get(self, evidence_id: UUID) -> Evidence | None:
        result = await self._session.scalars(
            select(Evidence).where(
                Evidence.organization_id == self.organization_id, Evidence.id == evidence_id
            )
        )
        return result.one_or_none()

    async def get_many(self, evidence_ids: Sequence[UUID]) -> Sequence[Evidence]:
        result = await self._session.scalars(
            select(Evidence).where(
                Evidence.organization_id == self.organization_id,
                Evidence.id.in_(evidence_ids),
            )
        )
        return result.all()

    async def link(
        self,
        *,
        fact_version_id: UUID,
        evidence_id: UUID,
        relation: EvidenceRelation,
        linked_by_user_id: UUID,
    ) -> bool:
        """Create the link if it does not exist yet. Returns True if a row was inserted."""
        inserted = await self._session.scalar(
            insert(FactVersionEvidence)
            .values(
                organization_id=self.organization_id,
                fact_version_id=fact_version_id,
                evidence_id=evidence_id,
                relation=relation,
                linked_by_user_id=linked_by_user_id,
            )
            .on_conflict_do_nothing(index_elements=["fact_version_id", "evidence_id"])
            .returning(FactVersionEvidence.evidence_id)
        )
        return inserted is not None

    async def get_link(
        self, fact_version_id: UUID, evidence_id: UUID
    ) -> FactVersionEvidence | None:
        result = await self._session.scalars(
            select(FactVersionEvidence).where(
                FactVersionEvidence.organization_id == self.organization_id,
                FactVersionEvidence.fact_version_id == fact_version_id,
                FactVersionEvidence.evidence_id == evidence_id,
            )
        )
        return result.one_or_none()

    async def evidence_for_version(
        self, fact_version_id: UUID
    ) -> Sequence[tuple[FactVersionEvidence, Evidence, FactSource]]:
        statement = (
            select(FactVersionEvidence, Evidence, FactSource)
            .join(
                Evidence,
                and_(
                    Evidence.organization_id == FactVersionEvidence.organization_id,
                    Evidence.id == FactVersionEvidence.evidence_id,
                ),
            )
            .join(
                FactSource,
                and_(
                    FactSource.organization_id == Evidence.organization_id,
                    FactSource.id == Evidence.source_id,
                ),
            )
            .where(
                FactVersionEvidence.organization_id == self.organization_id,
                FactVersionEvidence.fact_version_id == fact_version_id,
            )
            .order_by(FactVersionEvidence.linked_at, Evidence.captured_at, Evidence.id)
        )
        result = await self._session.execute(statement)
        return [(link, evidence, source) for link, evidence, source in result.tuples()]

    async def list_sources(self) -> Sequence[FactSource]:
        result = await self._session.scalars(
            select(FactSource)
            .where(FactSource.organization_id == self.organization_id)
            .order_by(FactSource.name)
        )
        return result.all()
