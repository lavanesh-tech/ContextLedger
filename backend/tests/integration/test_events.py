"""Domain events against PostgreSQL: outbox triggers, relay, idempotent consumers.

Kafka is replaced by an in-memory publisher here; test_kafka_roundtrip.py runs
the same path through a real broker.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.retrieval import RetrievalCache
from app.cache.store import MemoryStore
from app.domain.tenancy import TenantContext
from app.events.consumer import EventProcessor, Outcome, PermanentEventError, RetryPolicy
from app.events.handlers import ActivityProjector, RetrievalCacheInvalidator
from app.events.messages import IncomingMessage, OutgoingMessage
from app.events.schemas import EventEnvelope
from app.models.events import OrganizationActivityDaily, OutboxEvent, ProcessedEvent
from app.services.activity import ActivityService
from app.workers.event_relay import EventRelay
from tests.event_fakes import RecordingPublisher
from tests.integration.factories import admin_workspace, capture
from tests.integration.test_decisions import capture_context, decide, fact

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


async def outbox(sessions: Sessions, org: UUID) -> list[OutboxEvent]:
    async with sessions() as session:
        rows = await session.scalars(
            select(OutboxEvent).where(OutboxEvent.organization_id == org).order_by(OutboxEvent.id)
        )
        return list(rows.all())


async def relay(sessions: Sessions, org: UUID) -> list[OutgoingMessage]:
    publisher = RecordingPublisher()
    await EventRelay(sessions, publisher, batch_size=3, organization_id=org).drain()
    return publisher.messages


def incoming(message: OutgoingMessage, offset: int = 0) -> IncomingMessage:
    return IncomingMessage(message.topic, 0, offset, message.key, message.value, message.headers)


async def activity(sessions: Sessions, ctx: TenantContext) -> dict[str, int]:
    async with sessions() as session:
        report = await ActivityService(session).recent(
            ctx, days=366, today=datetime.now(UTC).date()
        )
    return {
        "facts": sum(d.facts_recorded for d in report.days),
        "evidence": sum(d.evidence_captured for d in report.days),
        "decisions": sum(d.decisions_recorded for d in report.days),
        "pending": report.pending_events,
    }


# --- outbox ----------------------------------------------------------------------------


async def test_every_change_writes_its_event_in_the_same_transaction(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    version = await fact(sessions, ctx, source, "credit_limit", 5000)
    evidence = await capture(sessions, ctx, source)
    receipt = await decide(sessions, ctx, await capture_context(sessions, ctx))

    events = await outbox(sessions, ctx.organization_id)

    assert [e.event_type for e in events] == [
        "fact.version_recorded",
        "evidence.captured",
        "context.captured",
        "decision.recorded",
    ]
    recorded = events[0]
    assert recorded.aggregate_id == version.id
    assert recorded.payload["version"] == 1 and recorded.payload["privacy_scope"] == "INTERNAL"
    assert "value" not in recorded.payload  # content never leaves through events
    assert events[1].aggregate_id == evidence.evidence.id
    assert "excerpt" not in events[1].payload
    assert events[3].aggregate_id == receipt.decision_id
    assert "outcome" not in events[3].payload and "rationale" not in events[3].payload
    assert len({e.event_id for e in events}) == 4


async def test_a_rolled_back_change_emits_nothing(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    before = len(await outbox(sessions, ctx.organization_id))

    async with sessions() as session:
        transaction = await session.begin()
        await session.execute(
            text(
                "INSERT INTO evidence (organization_id, source_id, evidence_type, excerpt, "
                "content_sha256, metadata, privacy_scope, captured_at) VALUES (:org, :src, "
                "'DOCUMENT_EXCERPT', 'x', repeat('c', 64), '{}', 'INTERNAL', now())"
            ),
            {"org": ctx.organization_id, "src": source.id},
        )
        inside = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.organization_id == ctx.organization_id)
        )
        await transaction.rollback()

    assert inside == before + 1  # the trigger wrote it inside the transaction

    assert len(await outbox(sessions, ctx.organization_id)) == before


# --- relay -----------------------------------------------------------------------------


async def test_the_relay_publishes_in_order_keyed_by_tenant_then_deletes(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)
    for value in (1000, 2000, 3000, 4000):
        await fact(sessions, ctx, source, f"p{value}", value)
    expected = [e.event_id for e in await outbox(sessions, ctx.organization_id)]

    messages = await relay(sessions, ctx.organization_id)

    envelopes = [EventEnvelope.model_validate_json(m.value) for m in messages]
    assert [e.id for e in envelopes] == expected  # outbox order, across batches of 3
    assert {m.topic for m in messages} == {"contextledger.facts.v1"}
    assert {m.key for m in messages} == {str(ctx.organization_id).encode()}
    assert dict(messages[0].headers)["event_type"] == b"fact.version_recorded"
    assert all(e.organization_id == ctx.organization_id for e in envelopes)
    assert await outbox(sessions, ctx.organization_id) == []


async def test_if_the_broker_fails_the_events_stay_queued(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)

    with pytest.raises(ConnectionError):
        await EventRelay(
            sessions, RecordingPublisher(fail=True), organization_id=ctx.organization_id
        ).run_once()

    assert len(await outbox(sessions, ctx.organization_id)) == 1
    assert len(await relay(sessions, ctx.organization_id)) == 1


# --- consumers --------------------------------------------------------------------------


async def test_redelivered_events_are_counted_once(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    await capture(sessions, ctx, source)
    await decide(sessions, ctx, await capture_context(sessions, ctx))
    messages = await relay(sessions, ctx.organization_id)
    processor = EventProcessor(ActivityProjector(), sessions, RecordingPublisher())

    first = [await processor.process(incoming(m, i)) for i, m in enumerate(messages)]
    again = [await processor.process(incoming(m, i)) for i, m in enumerate(messages)]

    assert first == [Outcome.HANDLED, Outcome.HANDLED, Outcome.IGNORED, Outcome.HANDLED]
    assert again == [Outcome.DUPLICATE, Outcome.DUPLICATE, Outcome.IGNORED, Outcome.DUPLICATE]
    assert await activity(sessions, ctx) == {
        "facts": 1,
        "evidence": 1,
        "decisions": 1,
        "pending": 0,
    }


async def test_concurrent_duplicates_apply_once(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    [message] = await relay(sessions, ctx.organization_id)
    processor = EventProcessor(ActivityProjector(), sessions, RecordingPublisher())

    outcomes = await asyncio.gather(*(processor.process(incoming(message)) for _ in range(5)))

    assert sorted(outcomes) == sorted([Outcome.HANDLED] + [Outcome.DUPLICATE] * 4)
    assert (await activity(sessions, ctx))["facts"] == 1


class Flaky:
    """Fails ``failures`` times, then works (or always fails with ``permanent``)."""

    name = "flaky-consumer"
    topics = ("contextledger.facts.v1",)
    event_types = frozenset({"fact.version_recorded"})

    def __init__(self, failures: int, *, permanent: bool = False) -> None:
        self.failures = failures
        self.permanent = permanent
        self.calls = 0

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        self.calls += 1
        await ActivityProjector().handle(session, event)  # a real effect, then fail
        if self.permanent:
            raise PermanentEventError("fact version no longer exists")
        if self.calls <= self.failures:
            raise RuntimeError("database hiccup")


async def no_sleep(seconds: float) -> None:
    return None


async def test_transient_failures_are_retried_and_roll_back_their_effects(
    sessions: Sessions,
) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    [message] = await relay(sessions, ctx.organization_id)
    handler, dlq = Flaky(failures=2), RecordingPublisher()

    outcome = await EventProcessor(
        handler, sessions, dlq, retry=RetryPolicy(max_attempts=3), sleep=no_sleep
    ).process(incoming(message))

    assert outcome is Outcome.HANDLED
    assert handler.calls == 3
    assert dlq.messages == []
    assert (await activity(sessions, ctx))["facts"] == 1  # the two failed attempts left nothing


async def test_exhausted_retries_go_to_the_dead_letter_topic(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    [message] = await relay(sessions, ctx.organization_id)
    dlq = RecordingPublisher()

    outcome = await EventProcessor(
        Flaky(failures=99), sessions, dlq, retry=RetryPolicy(max_attempts=3), sleep=no_sleep
    ).process(incoming(message, offset=41))

    assert outcome is Outcome.DEAD_LETTERED
    [dead] = dlq.messages
    headers = dict(dead.headers)
    assert dead.topic == "contextledger.facts.v1.dlq"
    assert dead.value == message.value
    assert headers["dlq_attempts"] == b"3"
    assert b"RuntimeError" in headers["dlq_error"]
    assert headers["dlq_source"] == b"contextledger.facts.v1/0/41"
    async with sessions() as session:
        recorded = await session.scalar(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(ProcessedEvent.organization_id == ctx.organization_id)
        )
    assert recorded == 0  # not marked processed: a replay from the DLQ will apply it


async def test_permanent_errors_are_not_retried(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    [message] = await relay(sessions, ctx.organization_id)
    handler, dlq = Flaky(failures=0, permanent=True), RecordingPublisher()

    outcome = await EventProcessor(handler, sessions, dlq, sleep=no_sleep).process(
        incoming(message)
    )

    assert outcome is Outcome.DEAD_LETTERED
    assert handler.calls == 1
    assert dict(dlq.messages[0].headers)["dlq_error"].startswith(b"permanent")


async def test_fact_events_invalidate_the_retrieval_cache(sessions: Sessions) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    [message] = await relay(sessions, ctx.organization_id)
    cache = RetrievalCache(MemoryStore())
    processor = EventProcessor(RetrievalCacheInvalidator(cache), sessions, RecordingPublisher())

    assert await processor.process(incoming(message)) is Outcome.HANDLED
    assert await cache.generation(ctx.organization_id) == 1


# --- the read model over REST ----------------------------------------------------------------


async def test_activity_is_eventually_consistent(
    sessions: Sessions, db_client: AsyncClient
) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 5000)
    url = f"/api/v1/organizations/{ctx.organization_id}/activity"
    headers = {"X-ContextLedger-User-Id": str(ctx.user_id)}

    before = (await db_client.get(url, headers=headers)).json()
    processor = EventProcessor(ActivityProjector(), sessions, RecordingPublisher())
    for message in await relay(sessions, ctx.organization_id):
        await processor.process(incoming(message))
    after = (await db_client.get(url, params={"days": 7}, headers=headers)).json()
    invalid = await db_client.get(url, params={"days": 0}, headers=headers)

    assert before["days"] == [] and before["pending_events"] == 1
    assert after["pending_events"] == 0
    assert [(d["facts_recorded"], d["decisions_recorded"]) for d in after["days"]] == [(1, 0)]
    assert invalid.status_code == 422


async def test_activity_rows_belong_to_their_tenant(sessions: Sessions) -> None:
    first, first_source = await admin_workspace(sessions)
    second, _ = await admin_workspace(sessions)
    await fact(sessions, first, first_source, "credit_limit", 5000)
    processor = EventProcessor(ActivityProjector(), sessions, RecordingPublisher())
    for message in await relay(sessions, first.organization_id):
        await processor.process(incoming(message))

    async with sessions() as session:
        rows = await session.scalars(
            select(OrganizationActivityDaily.organization_id).where(
                OrganizationActivityDaily.organization_id.in_(
                    [first.organization_id, second.organization_id]
                )
            )
        )
        owners = list(rows.all())
    assert owners == [first.organization_id]
    assert (await activity(sessions, second)) == {
        "facts": 0,
        "evidence": 0,
        "decisions": 0,
        "pending": 0,
    }
