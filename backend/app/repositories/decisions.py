"""Tenant-bound data access for context snapshots, decisions and receipts."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import ContextSnapshot, ContextSnapshotFact, Decision, DecisionFact
from app.models.entity import Entity
from app.models.fact import Fact, FactVersion
from app.models.source import FactSource


@dataclass(frozen=True, slots=True)
class SnapshotFactRow:
    snapshot_fact: ContextSnapshotFact
    version: FactVersion
    property: str
    entity_type: str
    external_id: str
    source_name: str


class DecisionRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self.organization_id = organization_id

    async def database_now(self) -> datetime:
        """The database clock (not transaction start), as used for transaction time."""
        result = await self._session.execute(select(func.clock_timestamp()))
        now: datetime = result.scalar_one()
        return now

    # --- snapshots ----------------------------------------------------------------

    async def add_snapshot(
        self, snapshot: ContextSnapshot, facts: list[ContextSnapshotFact]
    ) -> None:
        self._session.add(snapshot)
        await self._session.flush()
        self._session.add_all(facts)
        await self._session.flush()

    async def get_snapshot(self, snapshot_id: UUID) -> ContextSnapshot | None:
        result = await self._session.scalars(
            select(ContextSnapshot).where(
                ContextSnapshot.organization_id == self.organization_id,
                ContextSnapshot.id == snapshot_id,
            )
        )
        return result.one_or_none()

    async def snapshot_rows(self, snapshot_id: UUID) -> list[SnapshotFactRow]:
        statement = (
            select(
                ContextSnapshotFact,
                FactVersion,
                Fact.property,
                Entity.entity_type,
                Entity.external_id,
                FactSource.name,
            )
            .join(
                FactVersion,
                and_(
                    FactVersion.organization_id == ContextSnapshotFact.organization_id,
                    FactVersion.id == ContextSnapshotFact.fact_version_id,
                ),
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
            .join(
                FactSource,
                and_(
                    FactSource.organization_id == FactVersion.organization_id,
                    FactSource.id == FactVersion.source_id,
                ),
            )
            .where(
                ContextSnapshotFact.organization_id == self.organization_id,
                ContextSnapshotFact.snapshot_id == snapshot_id,
            )
            .order_by(ContextSnapshotFact.position)
        )
        result = await self._session.execute(statement)
        return [SnapshotFactRow(*row) for row in result.tuples()]

    # --- decisions ------------------------------------------------------------------

    async def add_decision(self, decision: Decision, facts: list[DecisionFact]) -> None:
        self._session.add(decision)
        await self._session.flush()
        self._session.add_all(facts)
        await self._session.flush()

    async def get_decision(self, decision_id: UUID) -> Decision | None:
        result = await self._session.scalars(
            select(Decision).where(
                Decision.organization_id == self.organization_id, Decision.id == decision_id
            )
        )
        return result.one_or_none()

    async def relied_on(self, decision_id: UUID) -> frozenset[UUID]:
        result = await self._session.scalars(
            select(DecisionFact.fact_version_id).where(
                DecisionFact.organization_id == self.organization_id,
                DecisionFact.decision_id == decision_id,
            )
        )
        return frozenset(result.all())

    async def decisions_relying_on(self, fact_version_id: UUID) -> Sequence[Decision]:
        result = await self._session.scalars(
            select(Decision)
            .join(
                DecisionFact,
                and_(
                    DecisionFact.organization_id == Decision.organization_id,
                    DecisionFact.decision_id == Decision.id,
                ),
            )
            .where(
                Decision.organization_id == self.organization_id,
                DecisionFact.fact_version_id == fact_version_id,
            )
            .order_by(Decision.decided_at, Decision.id)
        )
        return result.all()
