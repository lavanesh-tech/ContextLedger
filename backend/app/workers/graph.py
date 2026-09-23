"""Projects PostgreSQL provenance into Neo4j from the transactional outbox.

One iteration (``run_once``), inside ONE PostgreSQL transaction:

1. claim up to ``batch_size`` outbox events (``FOR UPDATE SKIP LOCKED``);
2. re-read the current rows they refer to (the outbox carries keys, not data);
3. write them to Neo4j in one Neo4j transaction (idempotent ``MERGE``);
4. delete the claimed events and commit.

If Neo4j fails, the PostgreSQL transaction rolls back and the events stay
queued. If the commit fails after Neo4j succeeded, the events are applied again
later, which changes nothing (idempotent). Delivery is at-least-once, the
effect is exactly-once.

Run:  python -m app.workers.graph            (loop)
      python -m app.workers.graph --once     (single batch)
"""

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.provenance.graph import GraphWriter, build_driver, ensure_schema
from app.provenance.projection import to_graph_row
from app.repositories.graph_outbox import GraphOutboxRepository

logger = logging.getLogger("contextledger.worker.graph")


class Writer(Protocol):
    async def apply(self, rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]]) -> int: ...


@dataclass(frozen=True, slots=True)
class ProjectionStats:
    events: int = 0
    rows: int = 0


class GraphProjector:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        writer: Writer,
        *,
        batch_size: int = 200,
        organization_id: UUID | None = None,
    ) -> None:
        self._sessions = sessions
        self._writer = writer
        self._batch_size = batch_size
        self._organization_id = organization_id  # optional scope (tests, re-projection)

    async def run_once(self) -> ProjectionStats:
        async with self._sessions() as session, session.begin():
            outbox = GraphOutboxRepository(session)
            events = await outbox.claim(
                batch_size=self._batch_size, organization_id=self._organization_id
            )
            if not events:
                return ProjectionStats()
            loaded = await outbox.load(events)
            rows = {table: [to_graph_row(r) for r in items] for table, items in loaded.items()}
            written = await self._writer.apply(rows)
            await outbox.delete(events)
        stats = ProjectionStats(events=len(events), rows=written)
        logger.info("graph.projected", extra={"events": stats.events, "rows": stats.rows})
        return stats

    async def drain(self) -> ProjectionStats:
        """Run until the (scoped) outbox is empty."""
        events = rows = 0
        while (stats := await self.run_once()).events:
            events += stats.events
            rows += stats.rows
        return ProjectionStats(events=events, rows=rows)

    async def run_forever(
        self,
        *,
        poll_interval: float,
        stop: asyncio.Event,
        heartbeat: Path | None = None,
        error_backoff: float = 15.0,
    ) -> None:
        while not stop.is_set():
            try:
                stats = await self.run_once()
            except Exception:
                logger.exception("graph.iteration_failed")
                await _sleep_unless_stopped(stop, error_backoff)
                continue
            if heartbeat is not None:
                await asyncio.to_thread(heartbeat.touch)
            if stats.events == 0:
                await _sleep_unless_stopped(stop, poll_interval)


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _main(settings: Settings, *, once: bool, heartbeat: Path | None) -> None:
    configure_logging(settings)
    engine = create_engine(settings)
    driver = build_driver(settings)
    try:
        await ensure_schema(driver, settings.neo4j_database)
        projector = GraphProjector(
            create_session_factory(engine),
            GraphWriter(driver, settings.neo4j_database),
            batch_size=settings.graph_batch_size,
        )
        logger.info("graph.projector_started", extra={"once": once})
        if once:
            await projector.drain()
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        if heartbeat is not None:
            await asyncio.to_thread(heartbeat.touch)
        await projector.run_forever(
            poll_interval=settings.graph_poll_interval_seconds, stop=stop, heartbeat=heartbeat
        )
        logger.info("graph.projector_stopped")
    finally:
        await driver.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextLedger provenance graph projector")
    parser.add_argument("--once", action="store_true", help="drain the outbox and exit")
    parser.add_argument("--heartbeat-file", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(_main(get_settings(), once=args.once, heartbeat=args.heartbeat_file))


if __name__ == "__main__":
    main()
