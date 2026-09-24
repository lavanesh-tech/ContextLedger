from uuid import UUID

import pytest

from app.cache.store import MemoryStore, StoreUnavailableError
from app.mcp.state import MAX_RECENT_QUERIES, McpSessionState
from tests.broken_store import BrokenStore

ORG, USER = UUID(int=1), UUID(int=2)


def state(store: object, session_id: str = "s1", agent: str = "agent") -> McpSessionState:
    return McpSessionState(
        store,  # type: ignore[arg-type]
        organization_id=ORG,
        user_id=USER,
        agent_name=agent,
        session_id=session_id,
    )


async def test_a_new_session_remembers_nothing() -> None:
    context = await state(MemoryStore()).current()
    assert context.session_id == "s1"
    assert context.last_snapshot_id is None
    assert context.snapshot_fact_version_ids == () and context.recent_queries == ()


async def test_snapshots_and_queries_are_remembered() -> None:
    session = state(MemoryStore())
    await session.remember_query("credit limit")
    await session.remember_snapshot(UUID(int=7), [UUID(int=8), UUID(int=9)])
    for i in range(MAX_RECENT_QUERIES + 2):
        await session.remember_query(f"q{i}")
    await session.remember_query("q5")  # repeated: moves to the front, no duplicate

    context = await session.current()

    assert context.last_snapshot_id == UUID(int=7)
    assert context.snapshot_fact_version_ids == (UUID(int=8), UUID(int=9))
    assert context.recent_queries[0] == "q5"
    assert len(context.recent_queries) == MAX_RECENT_QUERIES
    assert len(set(context.recent_queries)) == MAX_RECENT_QUERIES
    assert context.updated_at is not None


async def test_sessions_and_agents_are_isolated() -> None:
    store = MemoryStore()
    await state(store).remember_snapshot(UUID(int=7), [])
    assert (await state(store, session_id="s2").current()).last_snapshot_id is None
    assert (await state(store, agent="other").current()).last_snapshot_id is None


async def test_state_expires_after_inactivity() -> None:
    now = [0.0]
    store = MemoryStore(clock=lambda: now[0])
    session = McpSessionState(
        store, organization_id=ORG, user_id=USER, agent_name="a", session_id="s", ttl_seconds=60
    )
    await session.remember_snapshot(UUID(int=7), [])
    now[0] = 50.0
    await session.remember_query("keeps it alive")
    now[0] = 100.0
    assert (await session.current()).last_snapshot_id == UUID(int=7)
    now[0] = 200.0
    assert (await session.current()).last_snapshot_id is None


async def test_writes_tolerate_an_unavailable_store_but_reads_report_it() -> None:
    session = state(BrokenStore())
    await session.remember_query("q")
    await session.remember_snapshot(UUID(int=7), [])
    with pytest.raises(StoreUnavailableError):
        await session.current()
