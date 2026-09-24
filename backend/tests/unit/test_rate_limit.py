from app.cache.rate_limit import RateLimiter
from app.cache.store import MemoryStore
from tests.broken_store import BrokenStore


async def test_requests_over_the_limit_are_refused_until_the_window_resets() -> None:
    now = [0.0]
    limiter = RateLimiter(MemoryStore(clock=lambda: now[0]), window_seconds=60)

    decisions = [await limiter.hit("user:a", limit=3) for _ in range(4)]

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.remaining for d in decisions] == [2, 1, 0, 0]
    assert decisions[-1].retry_after_seconds == 60
    now[0] = 45.5
    assert (await limiter.hit("user:a", limit=3)).retry_after_seconds == 15
    now[0] = 60.0
    assert (await limiter.hit("user:a", limit=3)).allowed


async def test_subjects_have_separate_budgets() -> None:
    limiter = RateLimiter(MemoryStore())
    assert (await limiter.hit("user:a", limit=1)).allowed
    assert not (await limiter.hit("user:a", limit=1)).allowed
    assert (await limiter.hit("agent:b", limit=1)).allowed


async def test_an_unavailable_store_fails_open() -> None:
    decision = await RateLimiter(BrokenStore()).hit("user:a", limit=1)
    assert decision.allowed and decision.remaining == 1
