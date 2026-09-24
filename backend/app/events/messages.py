"""Transport-neutral message types, so relay and consumer logic is testable without Kafka."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.events.schemas import EventEnvelope, topic_for
from app.models.events import OutboxEvent


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    topic: str
    key: bytes
    value: bytes
    headers: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes
    headers: tuple[tuple[str, bytes], ...] = field(default=())


class Publisher(Protocol):
    async def publish(self, messages: Sequence[OutgoingMessage]) -> None:
        """Send all messages and wait until the broker acknowledged every one.
        Raises if any could not be delivered."""
        ...


def envelope_for(event: OutboxEvent) -> EventEnvelope:
    return EventEnvelope(
        id=event.event_id,
        type=event.event_type,
        organization_id=event.organization_id,
        subject=event.aggregate_id,
        time=event.occurred_at,
        data=event.payload,
    )


def message_for(event: OutboxEvent) -> OutgoingMessage:
    envelope = envelope_for(event)
    return OutgoingMessage(
        topic=topic_for(event.event_type),
        # Tenant id as key: one tenant's events share a partition, so they stay ordered.
        key=str(event.organization_id).encode(),
        value=envelope.model_dump_json().encode(),
        headers=(
            ("event_type", event.event_type.encode()),
            ("event_id", str(event.event_id).encode()),
        ),
    )
