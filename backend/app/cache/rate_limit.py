"""Distributed fixed-window rate limiting.

One counter per (subject, window) in the shared store: ``INCR`` plus a TTL set
when the window's key is created. Every API process increments the same counter,
so the limit holds across replicas.

Fixed windows allow up to twice the limit across a window boundary; that is an
accepted trade-off for one atomic round trip per request (see docs/REDIS.md).

If the store is unreachable the limiter **fails open** (the request proceeds and
a warning is logged): rate limiting protects capacity, and refusing every request
because the limiter is down would turn a Redis outage into a full API outage.
"""

import logging
import math
from dataclasses import dataclass

from app.cache.store import KeyValueStore, StoreUnavailableError

logger = logging.getLogger("contextledger.rate_limit")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int  # 0 when allowed


class RateLimiter:
    def __init__(self, store: KeyValueStore, *, window_seconds: int = 60) -> None:
        self._store = store
        self._window = window_seconds

    async def hit(self, subject: str, *, limit: int) -> RateLimitDecision:
        """Count one request for ``subject`` (e.g. "user:<id>") against ``limit``."""
        try:
            counter = await self._store.increment(
                f"cl:ratelimit:{subject}", ttl_seconds=self._window
            )
        except StoreUnavailableError as exc:
            logger.warning("rate_limit.store_unavailable", extra={"error": str(exc)})
            return RateLimitDecision(
                allowed=True, limit=limit, remaining=limit, retry_after_seconds=0
            )
        allowed = counter.value <= limit
        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - counter.value),
            retry_after_seconds=0 if allowed else max(1, math.ceil(counter.ttl_ms / 1000)),
        )
