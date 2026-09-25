"""The worker loop survives unexpected errors and only reports health after good iterations."""

import asyncio
from pathlib import Path
from typing import Any

from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.workers.embeddings import EmbeddingWorker, WorkerStats


class ScriptedWorker(EmbeddingWorker):
    """run_once replays a script of results/exceptions, then stops the loop."""

    def __init__(self, script: list[Any], stop: asyncio.Event) -> None:
        super().__init__(None, DeterministicHashEmbeddingProvider())  # type: ignore[arg-type]
        self.script = script
        self.stop = stop
        self.calls = 0

    async def run_once(self) -> WorkerStats:
        self.calls += 1
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, WorkerStats)
        return step


async def test_loop_survives_errors_and_keeps_going(tmp_path: Path) -> None:
    stop = asyncio.Event()
    heartbeat = tmp_path / "heartbeat"
    worker = ScriptedWorker(
        [RuntimeError("database restarting"), WorkerStats(claimed=3), WorkerStats()], stop
    )

    await worker.run_forever(poll_interval=0, stop=stop, heartbeat=heartbeat, error_backoff=0)

    assert worker.calls == 3
    assert heartbeat.exists()


async def test_no_heartbeat_while_iterations_fail(tmp_path: Path) -> None:
    stop = asyncio.Event()
    heartbeat = tmp_path / "heartbeat"
    worker = ScriptedWorker([RuntimeError("down"), RuntimeError("still down")], stop)

    await worker.run_forever(poll_interval=0, stop=stop, heartbeat=heartbeat, error_backoff=0)

    assert worker.calls == 2
    assert not heartbeat.exists()


class DrainWorker(EmbeddingWorker):
    def __init__(self, script: list[WorkerStats]) -> None:
        super().__init__(None, DeterministicHashEmbeddingProvider())  # type: ignore[arg-type]
        self.script = script
        self.calls = 0

    async def run_once(self) -> WorkerStats:
        self.calls += 1
        return self.script.pop(0)


async def test_drain_stops_when_nothing_is_pending_and_sums_the_batches() -> None:
    worker = DrainWorker(
        [
            WorkerStats(claimed=2, succeeded=2, embedded=2, tokens=10),
            WorkerStats(claimed=1, succeeded=0, failed=1),
            WorkerStats(),
            WorkerStats(claimed=9),
        ]
    )

    totals = await worker.drain(max_batches=10)

    assert worker.calls == 3
    assert totals == WorkerStats(claimed=3, succeeded=2, failed=1, embedded=2, tokens=10)


async def test_drain_respects_the_batch_limit() -> None:
    worker = DrainWorker([WorkerStats(claimed=1, succeeded=1) for _ in range(5)])

    totals = await worker.drain(max_batches=2)

    assert worker.calls == 2
    assert totals.succeeded == 2
