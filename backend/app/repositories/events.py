"""Event outbox, consumer deduplication and the activity read model."""

from collections.abc import Sequence
from datetime import date
from typing import Final
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import OrganizationActivityDaily, OutboxEvent, ProcessedEvent

ACTIVITY_COUNTERS: Final = frozenset({"facts_recorded", "evidence_captured", "decisions_recorded"})


class EventOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, *, batch_size: int, organization_id: UUID | None = None
    ) -> Sequence[OutboxEvent]:
        """Lock the oldest unpublished events (concurrent relays get disjoint sets)."""
        statement = (
            select(OutboxEvent)
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        if organization_id is not None:
            statement = statement.where(OutboxEvent.organization_id == organization_id)
        return (await self._session.scalars(statement)).all()

    async def delete(self, events: Sequence[OutboxEvent]) -> None:
        await self._session.execute(
            delete(OutboxEvent).where(OutboxEvent.id.in_([e.id for e in events]))
        )

    async def pending(self, organization_id: UUID | None = None) -> int:
        statement = select(func.count()).select_from(OutboxEvent)
        if organization_id is not None:
            statement = statement.where(OutboxEvent.organization_id == organization_id)
        return int(await self._session.scalar(statement) or 0)


class ProcessedEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, *, consumer: str, event_id: UUID, organization_id: UUID, event_type: str
    ) -> bool:
        """Record that ``consumer`` handles ``event_id`` in this transaction.
        False: it was already handled (a redelivery), so skip the effect."""
        inserted = await self._session.scalar(
            insert(ProcessedEvent)
            .values(
                consumer=consumer,
                event_id=event_id,
                organization_id=organization_id,
                event_type=event_type,
            )
            .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
            .returning(ProcessedEvent.event_id)
        )
        return inserted is not None


class ActivityRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID) -> None:
        self._session = session
        self._organization_id = organization_id

    async def increment(self, day: date, counter: str) -> None:
        if counter not in ACTIVITY_COUNTERS:
            raise ValueError(f"unknown activity counter {counter!r}")
        column = getattr(OrganizationActivityDaily, counter)
        await self._session.execute(
            insert(OrganizationActivityDaily)
            .values(organization_id=self._organization_id, day=day, **{counter: 1})
            .on_conflict_do_update(
                index_elements=["organization_id", "day"],
                set_={counter: column + 1, "updated_at": func.now()},
            )
        )

    async def recent(self, since: date) -> Sequence[OrganizationActivityDaily]:
        return (
            await self._session.scalars(
                select(OrganizationActivityDaily)
                .where(
                    OrganizationActivityDaily.organization_id == self._organization_id,
                    OrganizationActivityDaily.day >= since,
                )
                .order_by(OrganizationActivityDaily.day)
            )
        ).all()
