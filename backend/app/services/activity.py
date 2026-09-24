"""Tenant activity: a read model maintained asynchronously from Kafka events.

The numbers are eventually consistent: a change is counted after the relay has
published its event and the ``activity-projector`` consumer has processed it.
``pending_events`` says how many of this tenant's events are still waiting in
the outbox, so a caller can tell "zero" from "not counted yet".
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ValidationFailedError
from app.domain.roles import Permission
from app.domain.tenancy import TenantContext
from app.repositories.events import ActivityRepository, EventOutboxRepository
from app.services.authorization import require_permission

MAX_DAYS = 366


@dataclass(frozen=True, slots=True)
class ActivityDay:
    day: date
    facts_recorded: int
    evidence_captured: int
    decisions_recorded: int


@dataclass(frozen=True, slots=True)
class ActivityReport:
    since: date
    days: list[ActivityDay]
    pending_events: int  # still in the outbox, not yet counted


class ActivityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent(
        self, ctx: TenantContext, *, days: int, today: date | None = None
    ) -> ActivityReport:
        if not 1 <= days <= MAX_DAYS:
            raise ValidationFailedError(f"days must be between 1 and {MAX_DAYS}")
        since = (today or datetime.now(UTC).date()) - timedelta(days=days - 1)
        async with self._session.begin():
            await require_permission(self._session, ctx, Permission.READ_FACTS)
            rows = await ActivityRepository(self._session, ctx.organization_id).recent(since)
            pending = await EventOutboxRepository(self._session).pending(ctx.organization_id)
        return ActivityReport(
            since=since,
            days=[
                ActivityDay(r.day, r.facts_recorded, r.evidence_captured, r.decisions_recorded)
                for r in rows
            ],
            pending_events=pending,
        )
