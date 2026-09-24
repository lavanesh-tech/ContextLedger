"""Runs the event consumers against Kafka.

Each handler is its own consumer group (its own offsets): a slow or failing
consumer never holds back another. Messages are processed one at a time per
partition (``EventProcessor``), and offsets are committed after each batch of
processed messages, never before their effects are committed.

Run:  python -m app.workers.event_consumers
      python -m app.workers.event_consumers --only activity-projector
"""

import argparse
import asyncio
import logging
from pathlib import Path

from aiokafka import TopicPartition

from app.cache.retrieval import RetrievalCache
from app.cache.store import build_store
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.events.consumer import EventHandler, EventProcessor, RetryPolicy, next_offsets
from app.events.handlers import ActivityProjector, RetrievalCacheInvalidator
from app.events.kafka import (
    KafkaPublisher,
    build_consumer,
    build_producer,
    ensure_topics,
    to_incoming,
)
from app.workers.event_relay import sleep_unless_stopped, stop_on_signals

logger = logging.getLogger("contextledger.worker.event_consumers")


async def run_consumer(
    settings: Settings,
    handler: EventHandler,
    processor: EventProcessor,
    *,
    stop: asyncio.Event,
    heartbeat: Path | None = None,
) -> None:
    consumer = build_consumer(settings, group_id=handler.name, topics=handler.topics)
    await consumer.start()
    logger.info("events.consumer_started", extra={"consumer": handler.name})
    try:
        while not stop.is_set():
            batches = await consumer.getmany(timeout_ms=1000, max_records=100)
            processed = []
            for records in batches.values():
                for record in records:
                    message = to_incoming(record)
                    await processor.process(message)
                    processed.append(message)
            if processed:
                await consumer.commit(
                    {
                        TopicPartition(topic, partition): offset
                        for (topic, partition), offset in next_offsets(processed).items()
                    }
                )
            if heartbeat is not None:
                await asyncio.to_thread(heartbeat.touch)
    finally:
        await consumer.stop()


async def _supervise(
    settings: Settings,
    handler: EventHandler,
    processor: EventProcessor,
    stop: asyncio.Event,
    heartbeat: Path | None,
) -> None:
    """Restart a consumer after an error (uncommitted messages are redelivered)."""
    while not stop.is_set():
        try:
            await run_consumer(settings, handler, processor, stop=stop, heartbeat=heartbeat)
        except Exception:
            logger.exception("events.consumer_failed", extra={"consumer": handler.name})
            await sleep_unless_stopped(stop, 5.0)


def build_handlers(cache: RetrievalCache) -> list[EventHandler]:
    return [ActivityProjector(), RetrievalCacheInvalidator(cache)]


async def _main(settings: Settings, *, only: str | None, heartbeat: Path | None) -> None:
    configure_logging(settings)
    engine = create_engine(settings)
    store = build_store(settings)
    producer = build_producer(settings)  # dead-letter publisher
    try:
        await ensure_topics(settings)
        await producer.start()
        sessions = create_session_factory(engine)
        cache = RetrievalCache(store, ttl_seconds=settings.retrieval_cache_ttl_seconds)
        handlers = [h for h in build_handlers(cache) if only in (None, h.name)]
        if not handlers:
            raise SystemExit(f"no consumer named {only!r}")
        retry = RetryPolicy(
            max_attempts=settings.event_consumer_max_attempts,
            base_seconds=settings.event_consumer_retry_base_seconds,
        )
        stop = stop_on_signals()
        if heartbeat is not None:
            await asyncio.to_thread(heartbeat.touch)
        await asyncio.gather(
            *(
                _supervise(
                    settings,
                    handler,
                    EventProcessor(handler, sessions, KafkaPublisher(producer), retry=retry),
                    stop,
                    heartbeat,
                )
                for handler in handlers
            )
        )
        logger.info("events.consumers_stopped")
    finally:
        await producer.stop()
        await store.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextLedger event consumers (Kafka)")
    parser.add_argument("--only", default=None, help="run a single consumer by name")
    parser.add_argument("--heartbeat-file", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(_main(get_settings(), only=args.only, heartbeat=args.heartbeat_file))


if __name__ == "__main__":
    main()
