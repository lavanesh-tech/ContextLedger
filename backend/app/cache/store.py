"""Key-value store for short-lived, shared state (Redis).

Redis is never the system of record here: everything stored is either derived
from PostgreSQL (retrieval cache), protective (rate limits, idempotency keys),
or short-lived by design (OAuth state, MCP session state). Every key has a TTL.

Two implementations share one small interface:

* ``RedisStore``: used whenever ``CONTEXTLEDGER_REDIS_URL`` is set (required in
  staging and production), so several API processes share one view.
* ``MemoryStore``: per-process, for unit tests and for running the API without
  Redis on a laptop. It is correct for one process only.

The interface is deliberately narrow: each operation is atomic on its own, which
is what makes rate limiting and idempotency reservations race-free.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings


class StoreUnavailableError(Exception):
    """The store could not be reached or answered with an error."""


@dataclass(frozen=True, slots=True)
class Counter:
    value: int  # count after this increment
    ttl_ms: int  # milliseconds until the window resets


class KeyValueStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def set(
        self, key: str, value: bytes, *, ttl_seconds: float, only_if_absent: bool = False
    ) -> bool:
        """Store ``value``; with ``only_if_absent``, only if the key does not exist.
        Returns whether the value was written."""
        ...

    async def get_and_delete(self, key: str) -> bytes | None:
        """Atomically read and remove a key (single-use values)."""
        ...

    async def delete(self, key: str) -> None: ...

    async def increment(self, key: str, *, ttl_seconds: float) -> Counter:
        """Atomically add 1. The TTL is set when the key is created, never extended."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryStore:
    """In-process implementation with the same semantics (single process only)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[bytes, float]] = {}
        self._lock = asyncio.Lock()

    def _live(self, key: str) -> tuple[bytes, float] | None:
        entry = self._data.get(key)
        if entry is not None and entry[1] <= self._clock():
            del self._data[key]
            return None
        return entry

    async def get(self, key: str) -> bytes | None:
        entry = self._live(key)
        return None if entry is None else entry[0]

    async def set(
        self, key: str, value: bytes, *, ttl_seconds: float, only_if_absent: bool = False
    ) -> bool:
        async with self._lock:
            if only_if_absent and self._live(key) is not None:
                return False
            self._data[key] = (value, self._clock() + ttl_seconds)
            return True

    async def get_and_delete(self, key: str) -> bytes | None:
        async with self._lock:
            entry = self._live(key)
            self._data.pop(key, None)
            return None if entry is None else entry[0]

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def increment(self, key: str, *, ttl_seconds: float) -> Counter:
        async with self._lock:
            entry = self._live(key)
            now = self._clock()
            if entry is None:
                expires = now + ttl_seconds
                value = 1
            else:
                value, expires = int(entry[0]) + 1, entry[1]
            self._data[key] = (str(value).encode(), expires)
            return Counter(value=value, ttl_ms=max(0, round((expires - now) * 1000)))

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


async def _call(result: Awaitable[Any] | Any) -> Any:
    # redis-py annotates commands as "Awaitable[T] | T" (one class serves sync and
    # async clients); on the asyncio client they are always awaitable.
    return await cast(Awaitable[Any], result)


class RedisStore:
    """Redis implementation. Every Redis error becomes ``StoreUnavailableError`` so
    callers decide explicitly whether to fail open or closed."""

    def __init__(self, client: "Redis") -> None:
        self._redis = client

    @classmethod
    def from_url(cls, url: str, *, timeout_seconds: float) -> "RedisStore":
        return cls(
            Redis.from_url(
                url,
                socket_timeout=timeout_seconds,
                socket_connect_timeout=timeout_seconds,
                health_check_interval=30,
            )
        )

    async def get(self, key: str) -> bytes | None:
        try:
            return cast(bytes | None, await _call(self._redis.get(key)))
        except RedisError as exc:
            raise StoreUnavailableError(type(exc).__name__) from exc

    async def set(
        self, key: str, value: bytes, *, ttl_seconds: float, only_if_absent: bool = False
    ) -> bool:
        try:
            written = await _call(
                self._redis.set(key, value, px=_ms(ttl_seconds), nx=only_if_absent)
            )
        except RedisError as exc:
            raise StoreUnavailableError(type(exc).__name__) from exc
        return bool(written)

    async def get_and_delete(self, key: str) -> bytes | None:
        try:
            return cast(bytes | None, await _call(self._redis.getdel(key)))
        except RedisError as exc:
            raise StoreUnavailableError(type(exc).__name__) from exc

    async def delete(self, key: str) -> None:
        try:
            await _call(self._redis.delete(key))
        except RedisError as exc:
            raise StoreUnavailableError(type(exc).__name__) from exc

    async def increment(self, key: str, *, ttl_seconds: float) -> Counter:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.pexpire(key, _ms(ttl_seconds), nx=True)  # only when newly created
                pipe.pttl(key)
                value, _, ttl_ms = await pipe.execute()
        except RedisError as exc:
            raise StoreUnavailableError(type(exc).__name__) from exc
        return Counter(value=int(value), ttl_ms=max(0, int(ttl_ms)))

    async def ping(self) -> bool:
        try:
            return bool(await _call(self._redis.ping()))
        except RedisError:
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def _ms(seconds: float) -> int:
    return max(1, round(seconds * 1000))


def build_store(settings: Settings) -> KeyValueStore:
    url = settings.redis_url.get_secret_value()
    if not url:
        return MemoryStore()
    return RedisStore.from_url(url, timeout_seconds=settings.redis_timeout_seconds)
