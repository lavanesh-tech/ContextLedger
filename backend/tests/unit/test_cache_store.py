from collections.abc import Awaitable, Callable

import pytest

from app.cache.store import KeyValueStore, MemoryStore, build_store
from app.core.config import Environment, Settings
from tests.store_contract import CONTRACT


@pytest.mark.parametrize("check", CONTRACT, ids=lambda c: c.__name__)
async def test_memory_store_contract(check: Callable[[KeyValueStore], Awaitable[None]]) -> None:
    await check(MemoryStore())


async def test_memory_store_uses_its_clock() -> None:
    now = [100.0]
    store = MemoryStore(clock=lambda: now[0])
    await store.set("k", b"v", ttl_seconds=10)
    first = await store.increment("c", ttl_seconds=10)
    now[0] += 4
    assert (await store.increment("c", ttl_seconds=10)).ttl_ms == 6000
    now[0] += 7
    assert await store.get("k") is None
    assert first.value == 1 and (await store.increment("c", ttl_seconds=10)).value == 1


def test_without_a_redis_url_the_store_is_in_memory() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST)
    assert isinstance(build_store(settings), MemoryStore)
