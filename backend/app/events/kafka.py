"""aiokafka adapters: producer (publisher), consumer factory, topic administration.

Kept thin on purpose: relay and consumer logic depend on the ``Publisher`` and
``IncomingMessage`` types, not on aiokafka, and are unit-testable without a broker.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.core.config import Settings
from app.events.messages import IncomingMessage, OutgoingMessage
from app.events.schemas import TOPICS, dead_letter_topic

logger = logging.getLogger("contextledger.kafka")


def build_producer(settings: Settings) -> AIOKafkaProducer:
    # acks=all + idempotence: an acknowledged message is on every in-sync replica,
    # and the producer's own retries cannot create duplicates or reorder a partition.
    return AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id=f"{settings.kafka_client_id}-producer",
        acks="all",
        enable_idempotence=True,
        linger_ms=5,
        request_timeout_ms=int(settings.kafka_request_timeout_seconds * 1000),
    )


def build_consumer(settings: Settings, *, group_id: str, topics: Sequence[str]) -> AIOKafkaConsumer:
    # Offsets are committed manually, only after a message's effect is committed
    # (or it was dead-lettered): at-least-once delivery.
    return AIOKafkaConsumer(
        *topics,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id=f"{settings.kafka_client_id}-{group_id}",
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        isolation_level="read_committed",
    )


class KafkaPublisher:
    def __init__(self, producer: AIOKafkaProducer) -> None:
        self._producer = producer

    async def publish(self, messages: Sequence[OutgoingMessage]) -> None:
        # send() enqueues and returns a future per message; await all acknowledgements.
        pending = [
            await self._producer.send(m.topic, value=m.value, key=m.key, headers=list(m.headers))
            for m in messages
        ]
        await asyncio.gather(*pending)


def to_incoming(record: Any) -> IncomingMessage:
    return IncomingMessage(
        topic=str(record.topic),
        partition=int(record.partition),
        offset=int(record.offset),
        key=record.key,
        value=bytes(record.value),
        headers=tuple((str(k), bytes(v)) for k, v in (record.headers or ())),
    )


async def ensure_topics(settings: Settings) -> list[str]:
    """Create missing topics (and their dead-letter topics). Returns the names created."""
    wanted = dict.fromkeys(TOPICS, settings.kafka_topic_partitions)
    wanted |= {dead_letter_topic(topic): 1 for topic in TOPICS}
    admin = AIOKafkaAdminClient(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id=f"{settings.kafka_client_id}-admin",
    )
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = sorted(set(wanted) - existing)
        if missing:
            await admin.create_topics(
                [
                    NewTopic(
                        name=name,
                        num_partitions=wanted[name],
                        replication_factor=settings.kafka_replication_factor,
                    )
                    for name in missing
                ]
            )
            logger.info("kafka.topics_created", extra={"topics": missing})
        return missing
    finally:
        await admin.close()
