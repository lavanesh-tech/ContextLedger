"""Shared cache of retrieval results.

What is cached: the full ``RetrievalResult`` of one search, keyed by everything
that can change it:

* the organization and a per-organization **generation** number;
* the privacy scopes the caller may see (derived from the role *after* the
  permission check, so a hit never widens what anyone can read);
* the embedding model, the normalized query, limit, filters, trust weight;
* ``valid_at`` / ``known_at`` when the caller pinned them ("now" / "latest" otherwise).

Invalidation: writers that change what a search can return (recording a fact
version, the embedding worker storing vectors) bump the organization's
generation, which orphans every older entry at once. Entries also expire after
``retrieval_cache_ttl_seconds``, which bounds staleness for anything that does
not bump the generation (e.g. a script inserting facts directly in SQL), and for
"now" queries (a version whose ``valid_until`` passes stays cached up to the TTL).

Decision snapshots never use the cache: what a decision relied on is always
read from PostgreSQL.

Failure policy: fail open. If the store is down, every lookup is a miss, and
the search runs against PostgreSQL as if there were no cache.
"""

import hashlib
import json
import logging
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.cache.store import KeyValueStore, StoreUnavailableError
from app.domain.facts import PrivacyScope
from app.repositories.retrieval import RetrievalFilters
from app.services.retrieval import RetrievalResult

logger = logging.getLogger("contextledger.retrieval_cache")

_RESULT = TypeAdapter(RetrievalResult)
# Generations must outlive every entry that embeds them (entries live for minutes).
GENERATION_TTL_SECONDS = 7 * 24 * 3600
KEY_VERSION = "v1"


def _generation_key(organization_id: UUID) -> str:
    return f"cl:retrieval:gen:{organization_id}"


def _instant(value: datetime | None, default: str) -> str:
    return default if value is None else value.isoformat()


class RetrievalCache:
    def __init__(self, store: KeyValueStore, *, ttl_seconds: int = 60) -> None:
        self._store = store
        self._ttl = ttl_seconds

    async def generation(self, organization_id: UUID) -> int | None:
        """Current generation, or None when the store is unavailable (skip caching)."""
        try:
            raw = await self._store.get(_generation_key(organization_id))
        except StoreUnavailableError as exc:
            logger.warning("retrieval_cache.unavailable", extra={"error": str(exc)})
            return None
        return 0 if raw is None else int(raw)

    async def invalidate(self, organization_id: UUID) -> None:
        """Orphan every cached result of this organization."""
        try:
            await self._store.increment(
                _generation_key(organization_id), ttl_seconds=GENERATION_TTL_SECONDS
            )
        except StoreUnavailableError as exc:
            # Entries still expire after the TTL; staleness is bounded by it.
            logger.warning("retrieval_cache.invalidate_failed", extra={"error": str(exc)})

    @staticmethod
    def key(
        *,
        organization_id: UUID,
        generation: int,
        scopes: frozenset[PrivacyScope],
        model: str,
        query: str,
        limit: int,
        trust_weight: float,
        filters: RetrievalFilters,
        valid_at: datetime | None,
        known_at: datetime | None,
    ) -> str:
        material = json.dumps(
            {
                "scopes": sorted(scopes),
                "model": model,
                "query": query,
                "limit": limit,
                "trust_weight": trust_weight,
                "entity_type": filters.entity_type,
                "external_ids": list(filters.external_ids),
                "properties": list(filters.properties),
                "source_ids": [str(s) for s in filters.source_ids],
                "min_authority": filters.min_authority,
                "min_confidence": (
                    None if filters.min_confidence is None else str(filters.min_confidence)
                ),
                "valid_at": _instant(valid_at, "now"),
                "known_at": _instant(known_at, "latest"),
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"cl:retrieval:{KEY_VERSION}:{organization_id}:{generation}:{digest}"

    async def get(self, key: str) -> RetrievalResult | None:
        try:
            raw = await self._store.get(key)
        except StoreUnavailableError as exc:
            logger.warning("retrieval_cache.unavailable", extra={"error": str(exc)})
            return None
        if raw is None:
            return None
        try:
            return _RESULT.validate_json(raw)
        except ValidationError:
            # Written by an incompatible version: treat as a miss.
            logger.warning("retrieval_cache.undecodable_entry")
            return None

    async def put(self, key: str, result: RetrievalResult) -> None:
        try:
            await self._store.set(key, _RESULT.dump_json(result), ttl_seconds=self._ttl)
        except StoreUnavailableError as exc:
            logger.warning("retrieval_cache.unavailable", extra={"error": str(exc)})
