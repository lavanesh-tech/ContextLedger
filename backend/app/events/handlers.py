"""The consumers ContextLedger runs (python -m app.workers.event_consumers).

* ``ActivityProjector`` keeps ``organization_activity_daily`` (facts recorded,
  evidence captured, decisions recorded per tenant per UTC day). Counters are
  NOT idempotent, which is exactly why the dedup record matters: a redelivered
  event must not count twice.
* ``RetrievalCacheInvalidator`` bumps the tenant's retrieval-cache generation
  whenever a fact version or an embedding is stored, or a version is
  revoked. Because the events come
  from database triggers, this also covers writes that bypass the API (scripts,
  backfills), which the in-request invalidation of Phase 14 cannot see.
"""

from datetime import UTC
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.retrieval import RetrievalCache
from app.events.schemas import DECISIONS_TOPIC, EVIDENCE_TOPIC, FACTS_TOPIC, EventEnvelope
from app.repositories.events import ActivityRepository

COUNTER_BY_TYPE: Final = {
    "fact.version_recorded": "facts_recorded",
    "evidence.captured": "evidence_captured",
    "decision.recorded": "decisions_recorded",
}


class ActivityProjector:
    name = "activity-projector"
    topics = (FACTS_TOPIC, EVIDENCE_TOPIC, DECISIONS_TOPIC)
    event_types = frozenset(COUNTER_BY_TYPE)

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        # The event time is the commit-time now() of the change: bucket by UTC day.
        await ActivityRepository(session, event.organization_id).increment(
            event.time.astimezone(UTC).date(), COUNTER_BY_TYPE[event.type]
        )


class RetrievalCacheInvalidator:
    name = "retrieval-cache-invalidator"
    topics = (FACTS_TOPIC,)
    event_types = frozenset({"fact.version_recorded", "fact.embedding_stored", "fact.revoked"})

    def __init__(self, cache: RetrievalCache) -> None:
        self._cache = cache

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        # Idempotent on its own (a generation bump only orphans entries), and it
        # fails open: if Redis is down, entries still expire after their TTL.
        await self._cache.invalidate(event.organization_id)
