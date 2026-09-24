"""Idempotent event consumers with bounded retries and a dead-letter topic.

For every message (``EventProcessor.process``):

1. Decode the envelope. Undecodable or unknown-type messages cannot succeed
   by retrying, so they go straight to the dead-letter topic.
2. Skip types this handler does not subscribe to.
3. In ONE PostgreSQL transaction: insert ``(consumer, event_id)`` into
   ``processed_events`` (``ON CONFLICT DO NOTHING``) and apply the handler's
   effect. If the row already existed, this is a redelivery: nothing happens.
   Effect and dedup record commit or roll back together.
4. On a transient failure, retry with exponential backoff up to
   ``max_attempts``; a ``PermanentEventError`` is not retried. When retries are
   exhausted the message goes to ``<topic>.dlq`` with the error in its headers.

The Kafka offset is committed only after step 3 committed or the message was
dead-lettered (``run_consumer``). A crash in between redelivers the message,
and step 3 makes the redelivery harmless. Delivery is at-least-once; for the
PostgreSQL effect, the outcome is applied once. Effects outside PostgreSQL
(e.g. a Redis cache invalidation) must be idempotent on their own.

Retries block the partition they came from (ordering is kept); that is why
they are few and short. Longer outages end in the DLQ, from which messages can
be replayed after the cause is fixed.
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.messages import IncomingMessage, OutgoingMessage, Publisher
from app.events.schemas import EventEnvelope, UnknownEventTypeError, dead_letter_topic
from app.repositories.events import ProcessedEventRepository

logger = logging.getLogger("contextledger.events.consumer")


class PermanentEventError(Exception):
    """Retrying cannot help (e.g. the event references data that does not exist)."""


class EventHandler(Protocol):
    @property
    def name(self) -> str:
        """Consumer group id and dedup namespace."""
        ...

    @property
    def topics(self) -> tuple[str, ...]: ...

    @property
    def event_types(self) -> frozenset[str]: ...

    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        """Apply the effect inside the given transaction."""
        ...


class Outcome(StrEnum):
    HANDLED = "handled"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 0.5

    def delay(self, attempt: int) -> float:
        return float(self.base_seconds * 2 ** (attempt - 1))


class EventProcessor:
    def __init__(
        self,
        handler: EventHandler,
        sessions: async_sessionmaker[AsyncSession],
        dead_letters: Publisher,
        *,
        retry: RetryPolicy | None = None,
        sleep: "Sleep | None" = None,
    ) -> None:
        self._handler = handler
        self._sessions = sessions
        self._dead_letters = dead_letters
        self._retry = retry or RetryPolicy()
        self._sleep: Sleep = sleep or asyncio.sleep

    async def process(self, message: IncomingMessage) -> Outcome:
        try:
            event = EventEnvelope.model_validate_json(message.value)
            event.payload()  # validates the type and the data
        except (ValidationError, UnknownEventTypeError, ValueError) as exc:
            await self._dead_letter(message, f"undecodable: {type(exc).__name__}", attempts=0)
            return Outcome.DEAD_LETTERED
        if event.type not in self._handler.event_types:
            return Outcome.IGNORED

        error, attempt_count = "no attempt made", 0
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                return await self._apply(event)
            except PermanentEventError as exc:
                error = f"permanent: {exc}"
                attempt_count = attempt
                break
            except Exception as exc:  # transient until proven otherwise
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                attempt_count = attempt
                logger.warning(
                    "events.handler_failed",
                    extra={
                        "consumer": self._handler.name,
                        "event_id": str(event.id),
                        "event_type": event.type,
                        "attempt": attempt,
                        "error": error,
                    },
                )
                if attempt < self._retry.max_attempts:
                    await self._sleep(self._retry.delay(attempt))
        await self._dead_letter(message, error, attempts=attempt_count)
        return Outcome.DEAD_LETTERED

    async def _apply(self, event: EventEnvelope) -> Outcome:
        async with self._sessions() as session, session.begin():
            first_time = await ProcessedEventRepository(session).claim(
                consumer=self._handler.name,
                event_id=event.id,
                organization_id=event.organization_id,
                event_type=event.type,
            )
            if not first_time:
                return Outcome.DUPLICATE
            await self._handler.handle(session, event)
        return Outcome.HANDLED

    async def _dead_letter(self, message: IncomingMessage, error: str, *, attempts: int) -> None:
        # If this publish fails, the exception propagates, the offset is not
        # committed, and the message is delivered again later: nothing is lost.
        await self._dead_letters.publish(
            [
                OutgoingMessage(
                    topic=dead_letter_topic(message.topic),
                    key=message.key or b"",
                    value=message.value,
                    headers=(
                        *message.headers,
                        ("dlq_consumer", self._handler.name.encode()),
                        ("dlq_error", error[:500].encode()),
                        ("dlq_attempts", str(attempts).encode()),
                        (
                            "dlq_source",
                            f"{message.topic}/{message.partition}/{message.offset}".encode(),
                        ),
                    ),
                )
            ]
        )
        logger.error(
            "events.dead_lettered",
            extra={
                "consumer": self._handler.name,
                "topic": message.topic,
                "partition": message.partition,
                "offset": message.offset,
                "error": error,
            },
        )


class Sleep(Protocol):
    async def __call__(self, seconds: float, /) -> None: ...


def next_offsets(processed: Iterable[IncomingMessage]) -> Mapping[tuple[str, int], int]:
    """Offsets to commit: for each partition, one past the last processed message."""
    offsets: dict[tuple[str, int], int] = {}
    for message in processed:
        key = (message.topic, message.partition)
        offsets[key] = max(offsets.get(key, 0), message.offset + 1)
    return offsets
