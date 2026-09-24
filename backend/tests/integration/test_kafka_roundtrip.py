"""The event path through a real Kafka broker (``make up``; a service in CI)."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Environment, Settings
from app.events.consumer import EventProcessor, Outcome
from app.events.handlers import ActivityProjector
from app.events.kafka import (
    KafkaPublisher,
    build_consumer,
    build_producer,
    ensure_topics,
    to_incoming,
)
from app.events.messages import IncomingMessage
from app.events.schemas import FACTS_TOPIC, TOPICS, dead_letter_topic
from app.workers.event_relay import EventRelay
from tests.integration.factories import admin_workspace
from tests.integration.support import unavailable
from tests.integration.test_decisions import fact

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

KAFKA_ENV = "CONTEXTLEDGER_TEST_KAFKA_BOOTSTRAP_SERVERS"
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def kafka_settings() -> Settings:
    servers = os.environ.get(KAFKA_ENV)
    if not servers:
        unavailable(f"{KAFKA_ENV} is not set (use `make test` with `make up` running)")
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        kafka_bootstrap_servers=servers,
        kafka_client_id="contextledger-test",
    )
    try:
        async with asyncio.timeout(20):
            await ensure_topics(settings)
    except Exception as exc:
        unavailable(f"Kafka is not reachable ({type(exc).__name__}); start it with `make up`")
    return settings


async def test_events_travel_through_kafka_and_are_applied_once(
    sessions: Sessions, kafka_settings: Settings
) -> None:
    ctx, source = await admin_workspace(sessions)
    await fact(sessions, ctx, source, "credit_limit", 2000)
    await fact(sessions, ctx, source, "credit_limit", 5000, hours=5)
    key = str(ctx.organization_id).encode()

    producer = build_producer(kafka_settings)
    await producer.start()
    try:
        publisher = KafkaPublisher(producer)
        relayed = await EventRelay(sessions, publisher, organization_id=ctx.organization_id).drain()
    finally:
        await producer.stop()
    assert relayed.published == 2

    consumer = build_consumer(
        kafka_settings, group_id=f"test-{uuid4().hex[:8]}", topics=[FACTS_TOPIC]
    )
    await consumer.start()
    ours: list[IncomingMessage] = []
    try:
        async with asyncio.timeout(30):
            while len(ours) < 2:
                batches = await consumer.getmany(timeout_ms=500)
                ours += [
                    to_incoming(r) for records in batches.values() for r in records if r.key == key
                ]
    finally:
        await consumer.stop()

    assert len({m.partition for m in ours}) == 1  # one tenant, one partition, in order
    assert ours[0].offset < ours[1].offset
    processor = EventProcessor(ActivityProjector(), sessions, _NoDeadLetters())
    outcomes = [await processor.process(m) for m in ours + ours]  # simulate redelivery
    assert outcomes == [Outcome.HANDLED, Outcome.HANDLED, Outcome.DUPLICATE, Outcome.DUPLICATE]


async def test_topics_and_dead_letter_topics_exist(kafka_settings: Settings) -> None:
    consumer = AIOKafkaConsumer(bootstrap_servers=kafka_settings.kafka_bootstrap_servers)
    await consumer.start()
    try:
        topics = await consumer.topics()
    finally:
        await consumer.stop()
    producer = build_producer(kafka_settings)
    await producer.start()
    try:
        partitions = await producer.partitions_for(FACTS_TOPIC)  # fetches topic metadata
    finally:
        await producer.stop()
    assert set(TOPICS) | {dead_letter_topic(t) for t in TOPICS} <= topics
    assert len(partitions) == kafka_settings.kafka_topic_partitions
    assert await ensure_topics(kafka_settings) == []  # idempotent


async def test_dead_letters_reach_their_topic(kafka_settings: Settings) -> None:
    producer = build_producer(kafka_settings)
    await producer.start()
    marker = uuid4().hex.encode()
    try:
        processor = EventProcessor(ActivityProjector(), None, KafkaPublisher(producer))  # type: ignore[arg-type]
        outcome = await processor.process(IncomingMessage(FACTS_TOPIC, 0, 1, marker, b"garbage"))
    finally:
        await producer.stop()
    assert outcome is Outcome.DEAD_LETTERED

    consumer = build_consumer(
        kafka_settings,
        group_id=f"test-dlq-{uuid4().hex[:8]}",
        topics=[dead_letter_topic(FACTS_TOPIC)],
    )
    await consumer.start()
    found = None
    try:
        async with asyncio.timeout(30):
            while found is None:
                batches = await consumer.getmany(timeout_ms=500)
                found = next(
                    (r for records in batches.values() for r in records if r.key == marker), None
                )
    finally:
        await consumer.stop()
    assert found.value == b"garbage"
    assert dict(found.headers)["dlq_consumer"] == b"activity-projector"


class _NoDeadLetters:
    async def publish(self, messages: object) -> None:
        raise AssertionError("nothing should be dead-lettered")
