"""Temporary MCP session state (Redis, with a sliding TTL).

An MCP session is one agent conversation with this server. Across its tool calls
the server remembers, for up to ``mcp_session_ttl_seconds`` after the last call:

* the last context snapshot the agent captured and the fact_version_ids in it,
  so ``record_decision`` can default to "the snapshot I just captured";
* the last few search queries (for the agent's own orientation).

This is convenience state only. Nothing here is authoritative: the snapshot id
is re-validated by the decision service (tenant, permissions, citations) exactly
as if the agent had passed it explicitly, and losing the state (expiry, Redis
restart) only means the agent has to pass ``snapshot_id`` itself.

Keys are scoped to (organization, user, agent, session id), so two agents
never see each other's state.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.cache.store import KeyValueStore, StoreUnavailableError

logger = logging.getLogger("contextledger.mcp.state")

MAX_RECENT_QUERIES = 10


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_id: str
    last_snapshot_id: UUID | None
    snapshot_fact_version_ids: tuple[UUID, ...]
    recent_queries: tuple[str, ...]
    updated_at: datetime | None


class McpSessionState:
    def __init__(
        self,
        store: KeyValueStore,
        *,
        organization_id: UUID,
        user_id: UUID,
        agent_name: str,
        session_id: str,
        ttl_seconds: int = 3600,
    ) -> None:
        self._store = store
        self._key = f"cl:mcp:session:{organization_id}:{user_id}:{agent_name}:{session_id}"
        self._session_id = session_id
        self._ttl = ttl_seconds

    async def _load(self) -> dict[str, Any]:
        raw = await self._store.get(self._key)
        return {} if raw is None else dict(json.loads(raw))

    async def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = datetime.now(UTC).isoformat()
        await self._store.set(self._key, json.dumps(data).encode(), ttl_seconds=self._ttl)

    async def remember_query(self, query: str) -> None:
        try:
            data = await self._load()
            queries = [q for q in data.get("recent_queries", []) if q != query]
            data["recent_queries"] = [query, *queries][:MAX_RECENT_QUERIES]
            await self._save(data)
        except StoreUnavailableError as exc:
            logger.warning("mcp.state_unavailable", extra={"error": str(exc)})

    async def remember_snapshot(self, snapshot_id: UUID, fact_version_ids: list[UUID]) -> None:
        try:
            data = await self._load()
            data["last_snapshot_id"] = str(snapshot_id)
            data["snapshot_fact_version_ids"] = [str(v) for v in fact_version_ids]
            await self._save(data)
        except StoreUnavailableError as exc:
            logger.warning("mcp.state_unavailable", extra={"error": str(exc)})

    async def current(self) -> SessionContext:
        """The session's state (empty when nothing is remembered). Raises
        ``StoreUnavailableError`` if the store cannot be read."""
        data = await self._load()
        last = data.get("last_snapshot_id")
        updated = data.get("updated_at")
        return SessionContext(
            session_id=self._session_id,
            last_snapshot_id=None if last is None else UUID(last),
            snapshot_fact_version_ids=tuple(
                UUID(v) for v in data.get("snapshot_fact_version_ids", [])
            ),
            recent_queries=tuple(data.get("recent_queries", [])),
            updated_at=None if updated is None else datetime.fromisoformat(updated),
        )
