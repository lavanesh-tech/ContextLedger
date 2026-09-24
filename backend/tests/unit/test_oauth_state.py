import pytest

from app.cache.oauth_state import OAuthStateStore
from app.cache.store import MemoryStore, StoreUnavailableError
from tests.broken_store import BrokenStore


async def test_a_state_is_consumed_exactly_once() -> None:
    states = OAuthStateStore(MemoryStore())
    state = await states.issue({"client_id": "agent-1", "code_challenge": "abc"})

    assert len(state) >= 40
    assert await states.consume(state) == {"client_id": "agent-1", "code_challenge": "abc"}
    assert await states.consume(state) is None


async def test_unknown_empty_and_oversized_states_are_rejected() -> None:
    states = OAuthStateStore(MemoryStore())
    assert await states.consume("never-issued") is None
    assert await states.consume("") is None
    assert await states.consume("x" * 1000) is None


async def test_states_expire() -> None:
    now = [0.0]
    states = OAuthStateStore(MemoryStore(clock=lambda: now[0]), ttl_seconds=600)
    state = await states.issue({"n": 1})
    now[0] = 601.0
    assert await states.consume(state) is None


async def test_the_raw_state_is_never_a_key() -> None:
    store = MemoryStore()
    state = await OAuthStateStore(store).issue({})
    assert await store.get(state) is None
    assert await store.get(f"cl:oauth:state:{state}") is None


async def test_an_unavailable_store_fails_closed() -> None:
    with pytest.raises(StoreUnavailableError):
        await OAuthStateStore(BrokenStore()).consume("some-state")
