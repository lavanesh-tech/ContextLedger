"""Publishes the event outbox to Kafka (transactional outbox relay).

One iteration (``run_once``), inside ONE PostgreSQL transaction:

1. claim up to ``batch_size`` outbox rows in id order (``FOR UPDATE SKIP LOCKED``);
2. publish them to Kafka and wait until every message is acknowledged
   (``acks=all``, idempotent producer);
3. delete the rows and commit.

If Kafka fails, the transaction rolls back and the rows are published later.
If the commit fails after Kafka acknowledged, the rows are published again:
delivery is **at-least-once**, and consumers deduplicate by event id. With one
relay, each tenant's events reach their partition in commit order; several
relays still deliver every event, but may interleave one tenant's events.

Run:  python -m app.workers.event_relay            (loop)
      python -m app.workers.event_relay --once     (drain and exit)
"""

import argparse
import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.events.kafka import KafkaPublisher, build_producer, ensure_topics
from app.events.messages import Publisher, message_for
from app.repositories.events import EventOutboxRepository

logger = logging.getLogger("contextledger.worker.event_relay")


@dataclass(frozen=True, slots=True)
class RelayStats:
    published: int = 0


class EventRelay:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        batch_size: int = 200,
        organization_id: UUID | None = None,
    ) -> None:
        self._sessions = sessions
        self._publisher = publisher
        self._batch_size = batch_size
        self._organization_id = organization_id  # optional scope (tests)

    async def run_once(self) -> RelayStats:
        async with self._sessions() as session, session.begin():
            outbox = EventOutboxRepository(session)
            events = await outbox.claim(
                batch_size=self._batch_size, organization_id=self._organization_id
            )
            if not events:
                return RelayStats()
            await self._publisher.publish([message_for(event) for event in events])
            await outbox.delete(events)
        logger.info("events.relayed", extra={"published": len(events)})
        return RelayStats(published=len(events))

    async def drain(self) -> RelayStats:
        total = 0
        while (stats := await self.run_once()).published:
            total += stats.published
        return RelayStats(published=total)

    async def run_forever(
        self,
        *,
        poll_interval: float,
        stop: asyncio.Event,
        heartbeat: Path | None = None,
        error_backoff: float = 5.0,
    ) -> None:
        while not stop.is_set():
            try:
                stats = await self.run_once()
            except Exception:
                logger.exception("events.relay_failed")
                await sleep_unless_stopped(stop, error_backoff)
                continue
            if heartbeat is not None:
                await asyncio.to_thread(heartbeat.touch)
            if stats.published == 0:
                await sleep_unless_stopped(stop, poll_interval)


async def sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    return stop


async def _main(settings: Settings, *, once: bool, heartbeat: Path | None) -> None:
    configure_logging(settings)
    engine = create_engine(settings)
    producer = build_producer(settings)
    try:
        await ensure_topics(settings)
        await producer.start()
        relay = EventRelay(
            create_session_factory(engine),
            KafkaPublisher(producer),
            batch_size=settings.event_relay_batch_size,
        )
        logger.info("events.relay_started", extra={"once": once})
        if once:
            await relay.drain()
            return
        stop = stop_on_signals()
        if heartbeat is not None:
            await asyncio.to_thread(heartbeat.touch)
        await relay.run_forever(
            poll_interval=settings.event_relay_poll_interval_seconds,
            stop=stop,
            heartbeat=heartbeat,
        )
        logger.info("events.relay_stopped")
    finally:
        await producer.stop()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextLedger event outbox relay (Kafka)")
    parser.add_argument("--once", action="store_true", help="drain the outbox and exit")
    parser.add_argument("--heartbeat-file", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(_main(get_settings(), once=args.once, heartbeat=args.heartbeat_file))


if __name__ == "__main__":
    main()
