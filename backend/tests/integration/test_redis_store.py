"""RedisStore against a real Redis (``make up``): the same contract as MemoryStore,
plus the behaviours only a shared store can show."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from app.cache.rate_limit import RateLimiter
from app.cache.store import KeyValueStore, RedisStore, StoreUnavailableError
from tests.integration.support import unavailable
from tests.store_contract import CONTRACT, fresh

pytestmark = [pytest.mark.integration, pytest.mark.redis]

REDIS_URL_ENV = "CONTEXTLEDGER_TEST_REDIS_URL"


@pytest.fixture
async def redis_store() -> AsyncIterator[RedisStore]:
    url = os.environ.get(REDIS_URL_ENV)
    if not url:
        unavailable(f"{REDIS_URL_ENV} is not set (use `make test` with `make up` running)")
    store = RedisStore.from_url(url, timeout_seconds=2)
    if not await store.ping():
        await store.close()
        unavailable("Redis is not reachable; start it with `make up`")
    yield store
    await store.close()


@pytest.mark.parametrize("check", CONTRACT, ids=lambda c: c.__name__)
async def test_redis_store_contract(
    redis_store: RedisStore, check: Callable[[KeyValueStore], Awaitable[None]]
) -> None:
    await check(redis_store)


async def test_two_processes_share_one_budget(redis_store: RedisStore) -> None:
    url = os.environ[REDIS_URL_ENV]
    other_process = RedisStore.from_url(url, timeout_seconds=2)
    subject = fresh("shared")
    try:
        first = RateLimiter(redis_store)
        second = RateLimiter(other_process)
        allowed = [
            (await limiter.hit(subject, limit=4)).allowed
            for limiter in (first, second, first, second, first)
        ]
    finally:
        await other_process.close()
    assert allowed == [True, True, True, True, False]


async def test_an_unreachable_redis_raises_store_unavailable() -> None:
    store = RedisStore.from_url("redis://127.0.0.1:1/0", timeout_seconds=0.2)
    try:
        assert await store.ping() is False
        with pytest.raises(StoreUnavailableError):
            await store.get("k")
        with pytest.raises(StoreUnavailableError):
            await store.increment("k", ttl_seconds=1)
    finally:
        await store.close()
