"""Behaviour every KeyValueStore must have (run against MemoryStore and real Redis)."""

import asyncio
from uuid import uuid4

from app.cache.store import KeyValueStore


def fresh(name: str) -> str:
    return f"cl:test:{name}:{uuid4().hex}"


async def check_set_get_and_only_if_absent(store: KeyValueStore) -> None:
    key = fresh("set")
    assert await store.get(key) is None
    assert await store.set(key, b"one", ttl_seconds=30) is True
    assert await store.set(key, b"two", ttl_seconds=30, only_if_absent=True) is False
    assert await store.get(key) == b"one"
    assert await store.set(key, b"three", ttl_seconds=30) is True
    assert await store.get(key) == b"three"
    await store.delete(key)
    assert await store.get(key) is None


async def check_get_and_delete_is_single_use(store: KeyValueStore) -> None:
    key = fresh("getdel")
    await store.set(key, b"secret", ttl_seconds=30)
    results = await asyncio.gather(*(store.get_and_delete(key) for _ in range(5)))
    assert sorted(results, key=lambda r: r is None) == [b"secret", None, None, None, None]


async def check_concurrent_reservations_have_one_winner(store: KeyValueStore) -> None:
    key = fresh("nx")
    won = await asyncio.gather(
        *(store.set(key, b"x", ttl_seconds=30, only_if_absent=True) for _ in range(10))
    )
    assert won.count(True) == 1


async def check_increment_counts_and_keeps_its_window(store: KeyValueStore) -> None:
    key = fresh("incr")
    counters = await asyncio.gather(*(store.increment(key, ttl_seconds=30) for _ in range(20)))
    assert sorted(c.value for c in counters) == list(range(1, 21))
    assert all(0 < c.ttl_ms <= 30_000 for c in counters)
    later = await store.increment(key, ttl_seconds=3600)  # the TTL is not extended
    assert later.value == 21 and later.ttl_ms <= 30_000


async def check_entries_expire(store: KeyValueStore) -> None:
    key = fresh("ttl")
    await store.set(key, b"short", ttl_seconds=0.2)
    counter = fresh("ttl-counter")
    await store.increment(counter, ttl_seconds=0.2)
    await asyncio.sleep(0.35)
    assert await store.get(key) is None
    assert (await store.increment(counter, ttl_seconds=30)).value == 1


CONTRACT = (
    check_set_get_and_only_if_absent,
    check_get_and_delete_is_single_use,
    check_concurrent_reservations_have_one_winner,
    check_increment_counts_and_keeps_its_window,
    check_entries_expire,
)
