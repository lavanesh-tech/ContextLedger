"""EventProcessor paths that need no database: decoding, routing, offsets."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.consumer import EventProcessor, Outcome, RetryPolicy, next_offsets
from app.events.messages import IncomingMessage
from app.events.schemas import EventEnvelope
from tests.event_fakes import RecordingPublisher

ORG = UUID(int=1)


class OnlyDecisions:
    name = "test-consumer"
    topics = ("contextledger.decisions.v1",)
    event_types = frozenset({"decision.recorded"})

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        raise AssertionError("must not be called in these tests")


def message(value: bytes, offset: int = 7) -> IncomingMessage:
    return IncomingMessage(
        topic="contextledger.facts.v1",
        partition=2,
        offset=offset,
        key=str(ORG).encode(),
        value=value,
        headers=(("event_type", b"x"),),
    )


def processor(dlq: RecordingPublisher) -> EventProcessor:
    return EventProcessor(OnlyDecisions(), sessions=None, dead_letters=dlq)  # type: ignore[arg-type]


async def test_undecodable_messages_go_straight_to_the_dead_letter_topic() -> None:
    dlq = RecordingPublisher()

    outcome = await processor(dlq).process(message(b"{not json"))

    assert outcome is Outcome.DEAD_LETTERED
    [dead] = dlq.messages
    headers = dict(dead.headers)
    assert dead.topic == "contextledger.facts.v1.dlq"
    assert dead.key == str(ORG).encode() and dead.value == b"{not json"
    assert headers["dlq_consumer"] == b"test-consumer"
    assert headers["dlq_error"].startswith(b"undecodable")
    assert headers["dlq_attempts"] == b"0"
    assert headers["dlq_source"] == b"contextledger.facts.v1/2/7"
    assert headers["event_type"] == b"x"  # original headers are kept


async def test_unknown_event_types_are_dead_lettered() -> None:
    dlq = RecordingPublisher()
    event = EventEnvelope(
        id=UUID(int=5),
        type="fact.deleted",
        organization_id=ORG,
        subject=UUID(int=6),
        time=datetime(2026, 1, 15, tzinfo=UTC),
        data={},
    )
    assert await processor(dlq).process(message(event.model_dump_json().encode())) is (
        Outcome.DEAD_LETTERED
    )


async def test_events_the_handler_does_not_subscribe_to_are_ignored() -> None:
    dlq = RecordingPublisher()
    event = EventEnvelope(
        id=UUID(int=5),
        type="fact.embedding_stored",
        organization_id=ORG,
        subject=UUID(int=6),
        time=datetime(2026, 1, 15, tzinfo=UTC),
        data={
            "fact_version_id": str(UUID(int=6)),
            "model": "m",
            "created_at": "2026-01-15T00:00:00Z",
        },
    )
    assert (
        await processor(dlq).process(message(event.model_dump_json().encode())) is Outcome.IGNORED
    )
    assert dlq.messages == []


def test_offsets_commit_one_past_the_last_processed_message_per_partition() -> None:
    processed = [
        IncomingMessage("a", 0, 5, None, b""),
        IncomingMessage("a", 0, 6, None, b""),
        IncomingMessage("a", 1, 2, None, b""),
        IncomingMessage("b", 0, 9, None, b""),
    ]
    assert next_offsets(processed) == {("a", 0): 7, ("a", 1): 3, ("b", 0): 10}


def test_retry_delays_grow_exponentially() -> None:
    policy = RetryPolicy(max_attempts=4, base_seconds=0.5)
    assert [policy.delay(a) for a in (1, 2, 3)] == [0.5, 1.0, 2.0]
