"""MCP tool implementations: thin, typed adapters over the existing services.

Rules every tool follows:

* The **principal is fixed by configuration** (``McpIdentity``): no tool accepts
  an organization id or user id, so a model cannot switch tenants or pose as
  someone else. Membership is re-resolved on every call.
* The **agent has its own privacy ceiling**, which can only narrow the user's
  role (ADR-023).
* Tenant, time, permissions and provenance are decided by the services
  underneath, never by the model.
* Domain errors become clean tool errors. Anything unexpected is logged with
  details server-side and reported to the agent as "internal error".
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.retrieval import RetrievalCache
from app.cache.store import StoreUnavailableError
from app.domain.errors import DomainError
from app.domain.facts import PrivacyScope
from app.domain.retrieval import MAX_LIMIT, visible_privacy_scopes
from app.domain.tenancy import TenantContext
from app.mcp.serialization import to_jsonable
from app.mcp.state import McpSessionState
from app.provenance.graph import GraphReader
from app.providers.embeddings import EmbeddingProvider
from app.services.decisions import DecisionService, RecordDecision
from app.services.provenance import ProvenanceService
from app.services.retrieval import RetrievalQuery, RetrievalService
from app.services.temporal import TemporalService
from app.services.tenancy import TenancyService

logger = logging.getLogger("contextledger.mcp")

Json = dict[str, Any]
Instant = Annotated[
    datetime | None,
    Field(description="ISO-8601 instant with timezone, e.g. 2026-01-15T10:30:00Z"),
]


@dataclass(frozen=True, slots=True)
class McpIdentity:
    organization_id: UUID
    user_id: UUID
    agent_name: str
    max_privacy_scope: PrivacyScope


@dataclass(frozen=True, slots=True)
class McpRuntime:
    sessions: async_sessionmaker[AsyncSession]
    provider: EmbeddingProvider
    identity: McpIdentity
    graph: GraphReader | None = None  # None: impact tools report the graph as unavailable
    cache: RetrievalCache | None = None  # shared retrieval cache (Redis)
    state: McpSessionState | None = None  # temporary per-session state (Redis)


@asynccontextmanager
async def tool_errors(tool: str) -> AsyncIterator[None]:
    try:
        yield
    except DomainError as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
    except ToolError:
        raise
    except Exception as exc:
        logger.exception("mcp.tool_failed", extra={"tool": tool})
        raise ToolError("internal error; see the ContextLedger server logs") from exc


class ToolHandlers:
    """One bound method per MCP tool. Signatures become the tools' JSON schemas."""

    def __init__(self, runtime: McpRuntime) -> None:
        self._rt = runtime

    async def _tenant(self) -> TenantContext:
        identity = self._rt.identity
        async with self._rt.sessions() as session:
            return await TenancyService(session).resolve(
                organization_id=identity.organization_id, user_id=identity.user_id
            )

    def _visible(self, ctx: TenantContext) -> frozenset[PrivacyScope]:
        return visible_privacy_scopes(
            ctx.role, self._rt.identity.max_privacy_scope, ctx.max_privacy_scope
        )

    # --- retrieval ------------------------------------------------------------------

    async def search_facts(
        self,
        query: Annotated[str, Field(description="Natural-language question or keywords")],
        valid_at: Instant = None,
        known_at: Instant = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 10,
        entity_type: str | None = None,
        external_ids: Sequence[str] = (),
        properties: Sequence[str] = (),
    ) -> Json:
        """Hybrid search (vector + full text) over facts valid at `valid_at` as known at
        `known_at`, restricted to this tenant and the agent's privacy ceiling."""
        async with tool_errors("search_facts"):
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                result = await RetrievalService(
                    session, self._rt.provider, cache=self._rt.cache
                ).search(
                    ctx,
                    RetrievalQuery(
                        query=query,
                        limit=limit,
                        valid_at=valid_at,
                        known_at=known_at,
                        entity_type=entity_type,
                        external_ids=tuple(external_ids),
                        properties=tuple(properties),
                        max_privacy_scope=self._rt.identity.max_privacy_scope,
                    ),
                )
            if self._rt.state is not None:
                await self._rt.state.remember_query(result.query)
            return {
                "valid_at": to_jsonable(result.valid_at),
                "known_at": to_jsonable(result.known_at),
                "vector_search": result.vector_search,
                "cache": result.cache,
                "results": [
                    {
                        "fact_version_id": str(r.version.id),
                        "entity_type": r.entity_type,
                        "external_id": r.external_id,
                        "property": r.property,
                        "value": r.version.value,
                        "valid_from": to_jsonable(r.version.valid_from),
                        "valid_until": to_jsonable(r.version.valid_until),
                        "source": r.source_name,
                        "authority": r.version.authority,
                        "confidence": to_jsonable(r.version.confidence),
                        "score": round(r.ranking.score, 6),
                    }
                    for r in result.results
                ],
            }

    async def get_entity_facts(
        self,
        entity_type: str,
        external_id: str,
        valid_at: Instant = None,
        known_at: Instant = None,
    ) -> Json:
        """Every fact of one entity valid at `valid_at` (default now), as known at `known_at`."""
        async with tool_errors("get_entity_facts"):
            ctx = await self._tenant()
            visible = self._visible(ctx)
            async with self._rt.sessions() as session:
                facts = await TemporalService(session).facts_at(
                    ctx,
                    entity_type=entity_type,
                    external_id=external_id,
                    valid_at=valid_at,
                    known_at=known_at,
                )
            shown = [f for f in facts if f.version.privacy_scope in visible]
            return {
                "entity_type": entity_type,
                "external_id": external_id,
                "facts": [
                    {
                        "property": f.property,
                        "fact_id": str(f.fact_id),
                        "fact_version_id": str(f.version.id),
                        "value": f.version.value,
                        "valid_from": to_jsonable(f.version.valid_from),
                        "valid_until": to_jsonable(f.version.valid_until),
                        "recorded_at": to_jsonable(f.version.recorded_at),
                    }
                    for f in shown
                ],
                "withheld_by_privacy_scope": len(facts) - len(shown),
            }

    async def get_fact_history(
        self,
        entity_type: str,
        external_id: str,
        known_at: Instant = None,
    ) -> Json:
        """Timeline of every version of every fact of an entity, as known at `known_at`."""
        async with tool_errors("get_fact_history"):
            ctx = await self._tenant()
            visible = self._visible(ctx)
            async with self._rt.sessions() as session:
                timeline = await TemporalService(session).entity_timeline(
                    ctx, entity_type=entity_type, external_id=external_id, known_at=known_at
                )
            return {
                "entries": [
                    {
                        "property": e.property,
                        "fact_version_id": str(e.version.id),
                        "version": e.version.version,
                        "value": e.version.value,
                        "valid_from": to_jsonable(e.version.valid_from),
                        "valid_until": to_jsonable(e.version.valid_until),
                        "recorded_at": to_jsonable(e.version.recorded_at),
                    }
                    for e in timeline
                    if e.version.privacy_scope in visible
                ]
            }

    # --- decisions --------------------------------------------------------------------

    async def capture_decision_context(
        self,
        query: str,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 10,
        valid_at: Instant = None,
        known_at: Instant = None,
    ) -> Json:
        """Retrieve and FREEZE the facts you are about to decide with. Call this before
        record_decision; cite fact_version_ids from its result as `relied_on`."""
        async with tool_errors("capture_decision_context"):
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                captured = await DecisionService(session, self._rt.provider).capture_context(
                    ctx,
                    RetrievalQuery(
                        query=query,
                        limit=limit,
                        valid_at=valid_at,
                        known_at=known_at,
                        max_privacy_scope=self._rt.identity.max_privacy_scope,
                    ),
                )
            if self._rt.state is not None:
                await self._rt.state.remember_snapshot(
                    captured.snapshot_id, [r.version.id for r in captured.retrieval.results]
                )
            return {
                "snapshot_id": str(captured.snapshot_id),
                "valid_at": to_jsonable(captured.valid_at),
                "known_at": to_jsonable(captured.known_at),
                "facts": [
                    {
                        "fact_version_id": str(r.version.id),
                        "entity_type": r.entity_type,
                        "external_id": r.external_id,
                        "property": r.property,
                        "value": r.version.value,
                        "source": r.source_name,
                    }
                    for r in captured.retrieval.results
                ],
            }

    async def record_decision(
        self,
        action: Annotated[str, Field(description="Identifier, e.g. credit.approve_increase")],
        outcome: Annotated[Any, Field(description="JSON describing what was decided")],
        relied_on: Annotated[
            list[UUID], Field(description="fact_version_ids from the snapshot you relied on")
        ],
        rationale: str | None = None,
        snapshot_id: Annotated[
            UUID | None,
            Field(description="Defaults to the snapshot this session captured last"),
        ] = None,
    ) -> Json:
        """Record a decision made from a captured snapshot. Returns a verifiable receipt."""
        async with tool_errors("record_decision"):
            if snapshot_id is None:
                snapshot_id = await self._last_snapshot()
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                receipt = await DecisionService(session, self._rt.provider).record_decision(
                    ctx,
                    RecordDecision(
                        snapshot_id=snapshot_id,
                        action=action,
                        outcome=outcome,
                        relied_on=relied_on,
                        rationale=rationale,
                        agent=self._rt.identity.agent_name,
                    ),
                )
            return {
                "decision_id": str(receipt.decision_id),
                "receipt_sha256": receipt.receipt_sha256,
                "decided_at": to_jsonable(receipt.decided_at),
                "integrity_verified": receipt.integrity_verified,
            }

    async def get_decision_receipt(self, decision_id: UUID) -> Json:
        """The full receipt: decision, frozen context, facts as known then, evidence,
        and whether the stored hash still verifies."""
        async with tool_errors("get_decision_receipt"):
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                receipt = await DecisionService(session, self._rt.provider).receipt(
                    ctx, decision_id
                )
            visible = self._visible(ctx)
            document: Json = to_jsonable(receipt)
            for fact, raw in zip(document["facts"], receipt.facts, strict=True):
                # The receipt redacts by role; also apply this agent's own ceiling.
                if not raw.redacted and raw.privacy_scope not in visible:
                    fact.update(
                        redacted=True,
                        version=None,
                        entity_type=None,
                        external_id=None,
                        property=None,
                        source_name=None,
                        ranking={},
                        evidence=[],
                    )
            return document

    async def get_session_context(self) -> Json:
        """What this MCP session remembers: the last captured snapshot (and its
        fact_version_ids) and recent search queries. Expires after inactivity."""
        async with tool_errors("get_session_context"):
            if self._rt.state is None:
                raise ToolError("session state is not configured for this server")
            try:
                context = await self._rt.state.current()
            except StoreUnavailableError:
                raise ToolError("session state is temporarily unavailable") from None
            result: Json = to_jsonable(context)
            return result

    async def _last_snapshot(self) -> UUID:
        if self._rt.state is not None:
            try:
                context = await self._rt.state.current()
            except StoreUnavailableError:
                context = None
            if context is not None and context.last_snapshot_id is not None:
                return context.last_snapshot_id
        raise ToolError(
            "no snapshot_id given and none captured in this session; "
            "call capture_decision_context first or pass snapshot_id"
        )

    # --- provenance graph ---------------------------------------------------------------

    async def analyze_impact(
        self,
        kind: Literal["fact_version", "source", "evidence"],
        id: UUID,
    ) -> Json:
        """Which decisions depend on this fact version, source or evidence (and why)."""
        async with tool_errors("analyze_impact"):
            graph = self._graph()
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                service = ProvenanceService(session, graph)
                if kind == "fact_version":
                    report = await service.impact_of_fact_version(ctx, id)
                elif kind == "source":
                    report = await service.impact_of_source(ctx, id)
                else:
                    report = await service.impact_of_evidence(ctx, id)
            result: Json = to_jsonable(report)
            return result

    async def get_decision_lineage(self, decision_id: UUID) -> Json:
        """What a decision rests on: relied-on versions, their sources and evidence."""
        async with tool_errors("get_decision_lineage"):
            graph = self._graph()
            ctx = await self._tenant()
            async with self._rt.sessions() as session:
                lineage = await ProvenanceService(session, graph).decision_lineage(ctx, decision_id)
            result: Json = to_jsonable(lineage)
            return result

    def _graph(self) -> GraphReader:
        if self._rt.graph is None:
            raise ToolError("the provenance graph (Neo4j) is not configured for this server")
        return self._rt.graph
