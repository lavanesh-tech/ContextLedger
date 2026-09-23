"""Graph outbox: claim pending events and load the rows they refer to."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import ContextSnapshot, ContextSnapshotFact, Decision, DecisionFact
from app.models.entity import Entity
from app.models.evidence import Evidence, FactVersionEvidence
from app.models.fact import Fact, FactVersion
from app.models.outbox import GraphOutboxEvent
from app.models.source import FactSource

# table name -> (model, primary-key columns in row_key order)
PROJECTED: Final[dict[str, tuple[Any, tuple[str, ...]]]] = {
    "entities": (Entity, ("id",)),
    "facts": (Fact, ("id",)),
    "fact_sources": (FactSource, ("id",)),
    "fact_versions": (FactVersion, ("id",)),
    "evidence": (Evidence, ("id",)),
    "fact_version_evidence": (FactVersionEvidence, ("fact_version_id", "evidence_id")),
    "context_snapshots": (ContextSnapshot, ("id",)),
    "context_snapshot_facts": (ContextSnapshotFact, ("snapshot_id", "fact_version_id")),
    "decisions": (Decision, ("id",)),
    "decision_facts": (DecisionFact, ("decision_id", "fact_version_id")),
}


class GraphOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, *, batch_size: int, organization_id: UUID | None = None
    ) -> Sequence[GraphOutboxEvent]:
        """Lock the oldest pending events. Concurrent projectors get disjoint sets."""
        statement = (
            select(GraphOutboxEvent)
            .order_by(GraphOutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        if organization_id is not None:
            statement = statement.where(GraphOutboxEvent.organization_id == organization_id)
        return (await self._session.scalars(statement)).all()

    async def load(self, events: Sequence[GraphOutboxEvent]) -> dict[str, list[Any]]:
        """Current rows for the events, grouped by table (duplicates collapsed)."""
        keys: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        for event in events:
            if event.table_name not in PROJECTED:
                raise ValueError(f"graph outbox event for unknown table {event.table_name!r}")
            columns = PROJECTED[event.table_name][1]
            keys[event.table_name].add(tuple(str(event.row_key[c]) for c in columns))

        rows: dict[str, list[Any]] = {}
        for table, table_keys in keys.items():
            model, columns = PROJECTED[table]
            key_columns = [getattr(model, c) for c in columns]
            condition = (
                key_columns[0].in_([UUID(k[0]) for k in table_keys])
                if len(columns) == 1
                else tuple_(*key_columns).in_([tuple(UUID(part) for part in k) for k in table_keys])
            )
            rows[table] = list((await self._session.scalars(select(model).where(condition))).all())
        return rows

    async def delete(self, events: Sequence[GraphOutboxEvent]) -> None:
        await self._session.execute(
            delete(GraphOutboxEvent).where(GraphOutboxEvent.id.in_([e.id for e in events]))
        )

    async def pending(self, organization_id: UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(GraphOutboxEvent)
            .where(GraphOutboxEvent.organization_id == organization_id)
        )
        return int(count or 0)
