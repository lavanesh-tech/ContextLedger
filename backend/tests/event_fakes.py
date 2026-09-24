"""In-memory stand-ins for Kafka publishing."""

from collections.abc import Sequence

from app.events.messages import OutgoingMessage


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutgoingMessage] = []
        self.fail = fail

    async def publish(self, messages: Sequence[OutgoingMessage]) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.messages.extend(messages)
