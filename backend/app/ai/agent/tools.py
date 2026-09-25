"""Read-only investigator tools, built per request for one tenant.

Security model:

* The ``TenantContext`` is fixed by the server when the tools are built. No tool
  argument names an organization, user or scope, so the model cannot choose them.
* Arguments are validated by strict Pydantic schemas (``extra="forbid"``, UUIDs,
  bounded lengths) before any service is called.
* Every tool calls an existing service method, which re-checks membership,
  permission, token scopes and privacy ceilings. The agent gets no SQL and no
  repository access.
* Missing and forbidden look the same to the model ("not found or not
  accessible"), so tool errors do not reveal what exists in other tenants.
* Every id a tool returns is recorded in ``ToolLedger.observed``; the final
  answer may only cite observed ids.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import DomainError, NotFoundError, PermissionDeniedError
from app.domain.tenancy import TenantContext
from app.services.decisions import DecisionReceipt, DecisionService
from app.services.retrieval import RetrievalQuery, RetrievalResult, RetrievalService
from app.services.temporal import TemporalService
from app.temporal.model import Lineage, VersionSnapshot

NOT_ACCESSIBLE = "not found or not accessible"
MAX_VALUE_CHARS = 300


class InvestigatorBackend(Protocol):
    """The service calls the tools may make (implemented by ``ServiceBackend``)."""

    async def receipt(self, ctx: TenantContext, decision_id: UUID) -> DecisionReceipt: ...

    async def lineage(self, ctx: TenantContext, version_id: UUID) -> Lineage: ...

    async def decisions_relying_on(self, ctx: TenantContext, version_id: UUID) -> list[UUID]: ...

    async def search(self, ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult: ...


@dataclass
class ToolRecord:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    error: str | None
    latency_ms: int


@dataclass
class ToolLedger:
    observed: set[str] = field(default_factory=set)
    records: list[ToolRecord] = field(default_factory=list)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionArgs(_Args):
    decision_id: UUID = Field(description="The decision id.")


class FactVersionArgs(_Args):
    fact_version_id: UUID = Field(description="A fact version id returned by another tool.")


class SearchArgs(_Args):
    query: str = Field(min_length=1, max_length=300, description="What to search for.")
    valid_at: datetime | None = Field(
        default=None, description="ISO-8601 time with timezone; default now."
    )
    limit: int = Field(default=5, ge=1, le=10)


def _value(value: Any) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."


def _version(v: VersionSnapshot) -> dict[str, Any]:
    return {
        "fact_version_id": str(v.id),
        "version": v.version,
        "value": _value(v.value),
        "valid_from": v.valid_from.isoformat(),
        "valid_until": None if v.valid_until is None else v.valid_until.isoformat(),
        "recorded_at": v.recorded_at.isoformat(),
    }


def build_tools(
    ctx: TenantContext, backend: InvestigatorBackend, ledger: ToolLedger
) -> list[BaseTool]:
    def guarded(
        name: str, run: Callable[..., Awaitable[dict[str, Any]]]
    ) -> Callable[..., Awaitable[str]]:
        async def call(**arguments: Any) -> str:
            started = time.perf_counter()
            error: str | None = None
            try:
                payload = await run(**arguments)
            except (NotFoundError, PermissionDeniedError):
                error = NOT_ACCESSIBLE
            except DomainError as exc:
                error = str(exc)
            ledger.records.append(
                ToolRecord(
                    tool=name,
                    arguments={k: str(v) for k, v in arguments.items()},
                    ok=error is None,
                    error=error,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            if error is not None:
                return json.dumps({"error": error})
            return json.dumps(payload, default=str)

        return call

    async def get_decision_receipt(decision_id: UUID) -> dict[str, Any]:
        receipt = await backend.receipt(ctx, decision_id)
        ledger.observed.add(str(receipt.decision_id))
        facts = []
        for fact in receipt.facts:
            ledger.observed.add(str(fact.fact_version_id))
            if fact.redacted or fact.version is None:
                facts.append({"fact_version_id": str(fact.fact_version_id), "redacted": True})
                continue
            facts.append(
                {
                    **_version(fact.version),
                    "relied_on": fact.relied_on,
                    "revoked_at": None if fact.revoked_at is None else fact.revoked_at.isoformat(),
                    "entity": f"{fact.entity_type}:{fact.external_id}",
                    "property": fact.property,
                    "source": fact.source_name,
                }
            )
        return {
            "decision_id": str(receipt.decision_id),
            "action": receipt.action,
            "outcome": _value(receipt.outcome),
            "rationale": receipt.rationale,
            "agent": receipt.agent,
            "decided_at": receipt.decided_at.isoformat(),
            "context_known_at": receipt.context.known_at.isoformat(),
            "context_valid_at": receipt.context.valid_at.isoformat(),
            "integrity_verified": receipt.integrity_verified,
            "facts": facts,
        }

    async def get_fact_lineage(fact_version_id: UUID) -> dict[str, Any]:
        lineage = await backend.lineage(ctx, fact_version_id)
        chain = (lineage.version, *lineage.ancestors, *lineage.descendants)
        ledger.observed.update(str(v.id) for v in chain)
        return {
            "version": _version(lineage.version),
            "superseded": lineage.superseded_by is not None,
            "later_versions": [_version(v) for v in lineage.descendants],
            "earlier_versions": [_version(v) for v in lineage.ancestors],
        }

    async def find_decisions_relying_on(fact_version_id: UUID) -> dict[str, Any]:
        ids = [str(i) for i in await backend.decisions_relying_on(ctx, fact_version_id)]
        ledger.observed.update(ids)
        return {"fact_version_id": str(fact_version_id), "decision_ids": ids}

    async def search_facts(
        query: str, valid_at: datetime | None = None, limit: int = 5
    ) -> dict[str, Any]:
        result = await backend.search(
            ctx, RetrievalQuery(query=query, valid_at=valid_at, limit=limit)
        )
        rows = []
        for item in result.results:
            ledger.observed.add(str(item.version.id))
            rows.append(
                {
                    **_version(item.version),
                    "entity": f"{item.entity_type}:{item.external_id}",
                    "property": item.property,
                    "source": item.source_name,
                }
            )
        return {"valid_at": result.valid_at.isoformat(), "facts": rows}

    specs: list[tuple[str, str, type[_Args], Callable[..., Awaitable[dict[str, Any]]]]] = [
        (
            "get_decision_receipt",
            "The facts a decision saw when it was made, which it relied on, and its outcome.",
            DecisionArgs,
            get_decision_receipt,
        ),
        (
            "get_fact_lineage",
            "Earlier and later versions of a fact version; shows whether it was superseded.",
            FactVersionArgs,
            get_fact_lineage,
        ),
        (
            "find_decisions_relying_on",
            "Ids of decisions that relied on a fact version.",
            FactVersionArgs,
            find_decisions_relying_on,
        ),
        (
            "search_facts",
            "Search facts valid at a time (default now).",
            SearchArgs,
            search_facts,
        ),
    ]
    return [
        StructuredTool.from_function(
            coroutine=guarded(name, run),
            name=name,
            description=description,
            args_schema=schema,
        )
        for name, description, schema, run in specs
    ]


class ServiceBackend:
    """``InvestigatorBackend`` over the existing services (all checks stay in them)."""

    def __init__(
        self,
        *,
        decisions: DecisionService,
        temporal: TemporalService,
        retrieval: RetrievalService,
    ) -> None:
        self._decisions = decisions
        self._temporal = temporal
        self._retrieval = retrieval

    async def receipt(self, ctx: TenantContext, decision_id: UUID) -> DecisionReceipt:
        return await self._decisions.receipt(ctx, decision_id)

    async def lineage(self, ctx: TenantContext, version_id: UUID) -> Lineage:
        return await self._temporal.lineage(ctx, version_id)

    async def decisions_relying_on(self, ctx: TenantContext, version_id: UUID) -> list[UUID]:
        return await self._decisions.decisions_relying_on(ctx, version_id)

    async def search(self, ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        return await self._retrieval.search(ctx, query)
