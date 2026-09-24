from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.cache.retrieval import RetrievalCache
from app.cache.store import MemoryStore
from app.domain.facts import PrivacyScope, SourceType
from app.domain.retrieval import RankedCandidate
from app.repositories.retrieval import RetrievalFilters
from app.services.retrieval import RetrievalResult, RetrievedFact
from app.temporal.model import VersionSnapshot
from tests.broken_store import BrokenStore

ORG, OTHER_ORG = UUID(int=1), UUID(int=2)
T = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
INTERNAL = frozenset({PrivacyScope.PUBLIC, PrivacyScope.INTERNAL})


def key(**overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "organization_id": ORG,
        "generation": 0,
        "scopes": INTERNAL,
        "model": "deterministic-hash-v1",
        "query": "credit limit",
        "limit": 10,
        "trust_weight": 0.2,
        "filters": RetrievalFilters(),
        "valid_at": None,
        "known_at": None,
    }
    arguments.update(overrides)
    return RetrievalCache.key(**arguments)


def result() -> RetrievalResult:
    version = VersionSnapshot(
        id=UUID(int=10),
        fact_id=UUID(int=11),
        version=2,
        value={"amount": 5000, "currency": "USD"},
        source_id=UUID(int=12),
        valid_from=T,
        valid_until=None,
        observed_at=T,
        recorded_at=T,
        valid_until_recorded_at=None,
        supersedes_id=UUID(int=9),
        authority=90,
        confidence=Decimal("0.950"),
        privacy_scope=PrivacyScope.INTERNAL,
    )
    ranking = RankedCandidate(UUID(int=10), 1, 0.12, None, None, 0.016, 0.9, 0.031)
    return RetrievalResult(
        query="credit limit",
        valid_at=T,
        known_at=None,
        vector_search="used",
        embedding_model="deterministic-hash-v1",
        privacy_scopes=(PrivacyScope.PUBLIC, PrivacyScope.INTERNAL),
        vector_candidates=1,
        text_candidates=0,
        results=[
            RetrievedFact(
                version,
                "customer",
                "customer-991",
                "credit_limit",
                "billing",
                SourceType.SYSTEM_OF_RECORD,
                ranking,
            )
        ],
        cache="miss",
    )


def test_everything_that_changes_an_answer_changes_the_key() -> None:
    baseline = key()
    variants = [
        key(organization_id=OTHER_ORG),
        key(generation=1),
        key(scopes=INTERNAL | {PrivacyScope.CONFIDENTIAL}),
        key(model="text-embedding-3-small"),
        key(query="credit limits"),
        key(limit=11),
        key(trust_weight=0.3),
        key(filters=RetrievalFilters(entity_type="customer")),
        key(filters=RetrievalFilters(min_confidence=Decimal("0.5"))),
        key(valid_at=T),
        key(known_at=T),
    ]
    assert baseline == key()
    assert len({baseline, *variants}) == len(variants) + 1
    assert str(ORG) in baseline and "credit" not in baseline  # the query is hashed


async def test_results_round_trip_exactly() -> None:
    cache = RetrievalCache(MemoryStore())
    await cache.put("k", result())
    assert await cache.get("k") == result()


async def test_invalidation_moves_the_generation() -> None:
    cache = RetrievalCache(MemoryStore())
    assert await cache.generation(ORG) == 0
    await cache.invalidate(ORG)
    await cache.invalidate(ORG)
    assert await cache.generation(ORG) == 2
    assert await cache.generation(OTHER_ORG) == 0


async def test_entries_expire() -> None:
    now = [0.0]
    cache = RetrievalCache(MemoryStore(clock=lambda: now[0]), ttl_seconds=60)
    await cache.put("k", result())
    now[0] = 61.0
    assert await cache.get("k") is None


async def test_undecodable_entries_are_misses() -> None:
    store = MemoryStore()
    await store.set("k", b'{"not": "a result"}', ttl_seconds=60)
    assert await RetrievalCache(store).get("k") is None


async def test_an_unavailable_store_disables_caching_without_failing() -> None:
    cache = RetrievalCache(BrokenStore())
    assert await cache.generation(ORG) is None
    assert await cache.get("k") is None
    await cache.put("k", replace(result()))
    await cache.invalidate(ORG)
