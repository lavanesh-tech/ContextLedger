"""MCP tools end to end: FastMCP → tool handlers → services → PostgreSQL (and Neo4j)."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import FakeGenerationProvider, GenerationUnavailableError, ToolCall
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.mcp.server import build_server
from app.mcp.tools import McpIdentity, McpRuntime, ToolHandlers
from app.models.agent_run import AgentRun
from app.models.source import FactSource
from app.providers.embeddings import DeterministicHashEmbeddingProvider
from app.services.facts import RecordFactVersion
from app.services.memberships import MembershipService
from tests.integration.conftest import GraphHarness
from tests.integration.factories import add_member, admin_workspace, make_user, record

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROVIDER = DeterministicHashEmbeddingProvider()


async def fact(
    sessions: Sessions,
    ctx: TenantContext,
    source: FactSource,
    prop: str,
    value: object,
    *,
    hours: int = 0,
    scope: PrivacyScope = PrivacyScope.INTERNAL,
) -> Any:
    return await record(
        sessions,
        ctx,
        RecordFactVersion(
            entity_type="customer",
            external_id="customer-991",
            property=prop,
            value=value,
            source_id=source.id,
            valid_from=T0 + timedelta(hours=hours),
            privacy_scope=scope,
        ),
    )


async def agent(
    sessions: Sessions,
    *,
    role: MembershipRole = MembershipRole.ENGINEER,
    ceiling: PrivacyScope = PrivacyScope.INTERNAL,
    graph: GraphHarness | None = None,
) -> tuple[ToolHandlers, TenantContext, FactSource, Any]:
    """An organization with facts, and MCP tools acting as an agent user in it."""
    admin_ctx, source = await admin_workspace(sessions)
    await fact(sessions, admin_ctx, source, "credit_limit", 2000)
    await fact(sessions, admin_ctx, source, "credit_limit", 5000, hours=5)
    await fact(
        sessions, admin_ctx, source, "internal_risk_notes", "watch", scope=PrivacyScope.CONFIDENTIAL
    )
    bot = await make_user(sessions)
    await add_member(sessions, admin_ctx, bot, role)
    runtime = McpRuntime(
        sessions=sessions,
        provider=PROVIDER,
        identity=McpIdentity(admin_ctx.organization_id, bot.id, "credit-review-agent", ceiling),
        graph=None if graph is None else graph.reader,
    )
    return ToolHandlers(runtime), admin_ctx, source, bot


def structured(result: Any) -> dict[str, Any]:
    """FastMCP.call_tool returns (content, structured) or content blocks depending on version."""
    if isinstance(result, tuple):
        result = result[1] if isinstance(result[1], dict) else result[0]
    if isinstance(result, dict):
        return result.get("result", result) if set(result) == {"result"} else result
    return dict(json.loads(result[0].text))


# --- reading ------------------------------------------------------------------------


async def test_search_respects_time_and_the_agents_privacy_ceiling(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)

    now = await tools.search_facts("credit limit risk notes customer-991")
    past = await tools.search_facts("credit limit", valid_at=T0 + timedelta(hours=1))

    assert [(r["property"], r["value"]) for r in now["results"]] == [("credit_limit", 5000)]
    assert [r["value"] for r in past["results"]] == [2000]
    assert now["vector_search"] == "used"


async def test_entity_facts_report_what_was_withheld(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)
    wider, _, _, _ = await agent(sessions, ceiling=PrivacyScope.CONFIDENTIAL)

    narrow = await tools.get_entity_facts("customer", "customer-991")
    wide = await wider.get_entity_facts("customer", "customer-991")

    assert {f["property"] for f in narrow["facts"]} == {"credit_limit"}
    assert narrow["withheld_by_privacy_scope"] == 1
    assert {f["property"] for f in wide["facts"]} == {"credit_limit", "internal_risk_notes"}


async def test_fact_history_lists_every_version(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)

    history = await tools.get_fact_history("customer", "customer-991")

    assert [(e["version"], e["value"]) for e in history["entries"]] == [(1, 2000), (2, 5000)]


# --- deciding -------------------------------------------------------------------------


async def test_capture_decide_and_prove(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)

    context = await tools.capture_decision_context("credit limit customer-991")
    limit = next(f for f in context["facts"] if f["property"] == "credit_limit")
    decision = await tools.record_decision(
        snapshot_id=UUID(context["snapshot_id"]),
        action="credit.approve_increase",
        outcome={"approved": True, "new_limit": 7500},
        relied_on=[UUID(limit["fact_version_id"])],
        rationale="Limit is 5000 and has been stable.",
    )
    receipt = await tools.get_decision_receipt(UUID(decision["decision_id"]))

    assert decision["integrity_verified"] is True
    assert receipt["integrity_verified"] is True
    assert receipt["agent"] == "credit-review-agent"
    assert receipt["outcome"] == {"approved": True, "new_limit": 7500}
    relied = [f for f in receipt["facts"] if f["relied_on"]]
    assert [f["version"]["value"] for f in relied] == [5000]


async def test_viewers_cannot_record_decisions(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions, role=MembershipRole.VIEWER)

    with pytest.raises(ToolError, match="PermissionDeniedError"):
        await tools.capture_decision_context("credit limit")


async def test_invalid_input_is_a_clean_tool_error(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)
    context = await tools.capture_decision_context("credit limit")

    with pytest.raises(ToolError, match="ValidationFailedError"):
        await tools.record_decision(
            snapshot_id=UUID(context["snapshot_id"]),
            action="Approve!!",
            outcome={"approved": True},
            relied_on=[],
        )


async def test_revoked_membership_takes_effect_immediately(sessions: Sessions) -> None:
    tools, admin_ctx, _, bot = await agent(sessions)
    await tools.search_facts("credit limit")

    async with sessions() as session:
        await MembershipService(session).remove_member(admin_ctx, user_id=bot.id)

    with pytest.raises(ToolError, match="PermissionDeniedError"):
        await tools.search_facts("credit limit")


# --- provenance graph -------------------------------------------------------------------


async def test_impact_tools_need_the_graph(sessions: Sessions) -> None:
    tools, _, source, _ = await agent(sessions)

    with pytest.raises(ToolError, match="not configured"):
        await tools.analyze_impact("source", source.id)


@pytest.mark.graph
async def test_impact_through_the_graph(sessions: Sessions, graph: GraphHarness) -> None:
    tools, admin_ctx, source, _ = await agent(sessions, graph=graph)
    context = await tools.capture_decision_context("credit limit customer-991")
    limit = next(f for f in context["facts"] if f["property"] == "credit_limit")
    decision = await tools.record_decision(
        snapshot_id=UUID(context["snapshot_id"]),
        action="credit.approve_increase",
        outcome={"approved": True},
        relied_on=[UUID(limit["fact_version_id"])],
    )
    await graph.projector(sessions, admin_ctx.organization_id).drain()

    impact = await tools.analyze_impact("source", source.id)
    lineage = await tools.get_decision_lineage(UUID(decision["decision_id"]))

    assert [d["decision_id"] for d in impact["decisions"]] == [decision["decision_id"]]
    assert lineage["relied_on"][0]["fact_version_id"] == limit["fact_version_id"]


# --- through the MCP server ---------------------------------------------------------------


async def test_tools_are_callable_through_fastmcp(sessions: Sessions) -> None:
    _, admin_ctx, _, bot = await agent(sessions)
    runtime = McpRuntime(
        sessions=sessions,
        provider=PROVIDER,
        identity=McpIdentity(admin_ctx.organization_id, bot.id, "agent", PrivacyScope.INTERNAL),
    )
    server = build_server(runtime)

    result = structured(await server.call_tool("search_facts", {"query": "credit limit"}))

    assert [r["value"] for r in result["results"]] == [5000]
    with pytest.raises(ToolError):
        await server.call_tool("get_decision_receipt", {"decision_id": "not-a-uuid"})


# --- AI tools (scripted model: no network, no key) ------------------------------------------


def with_model(tools: ToolHandlers, model: FakeGenerationProvider) -> ToolHandlers:
    return ToolHandlers(dataclasses.replace(tools._rt, generator=model))


async def test_answer_question_cites_only_facts_under_the_agents_ceiling(
    sessions: Sessions,
) -> None:
    base, _, _, _ = await agent(sessions)
    model = FakeGenerationProvider(
        responder=lambda _: json.dumps(
            {
                "answer": "5000",
                "insufficient_evidence": False,
                "cited_facts": ["F1"],
                "inferences": [],
            }
        )
    )
    tools = with_model(base, model)

    answer = await tools.answer_question("credit limit and internal risk notes of customer-991")

    assert answer["status"] == "answered" and answer["answer"] == "5000"
    prompt = model.requests[0].messages[-1].content
    assert "credit_limit" in prompt
    assert "internal_risk_notes" not in prompt  # CONFIDENTIAL: above the agent's ceiling


async def test_investigate_decision_runs_the_agent_and_stores_a_trace(sessions: Sessions) -> None:
    base, _, _, _ = await agent(sessions)
    context = await base.capture_decision_context("credit limit customer-991")
    version = next(f for f in context["facts"] if f["property"] == "credit_limit")[
        "fact_version_id"
    ]
    decision = await base.record_decision(
        snapshot_id=UUID(context["snapshot_id"]),
        action="credit.approve_increase",
        outcome={"approved": True},
        relied_on=[UUID(version)],
    )
    model = FakeGenerationProvider(
        responses=[
            [ToolCall("c1", "get_decision_receipt", {"decision_id": decision["decision_id"]})],
            json.dumps(
                {
                    "answer": "Approved on the 5000 limit.",
                    "insufficient_evidence": False,
                    "cited_ids": [decision["decision_id"], version],
                }
            ),
        ]
    )
    tools = with_model(base, model)

    run = await tools.investigate_decision("Why was the increase approved?")

    assert run["status"] == "answered"
    assert run["cited_ids"] == [decision["decision_id"], version]
    assert [c["tool"] for c in run["tool_calls"]] == ["get_decision_receipt"]
    async with sessions() as session:
        stored = await session.get(AgentRun, UUID(run["run_id"]))
    assert stored is not None and stored.status == "answered"


async def test_ai_tools_fail_cleanly_without_a_model(sessions: Sessions) -> None:
    tools, _, _, _ = await agent(sessions)
    with pytest.raises(ToolError, match="disabled"):
        await tools.answer_question("credit limit")
    with pytest.raises(ToolError, match="disabled"):
        await tools.investigate_decision("why?")


async def test_a_model_failure_is_a_tool_error_not_an_answer(sessions: Sessions) -> None:
    base, _, _, _ = await agent(sessions)
    tools = with_model(base, FakeGenerationProvider(responses=[GenerationUnavailableError("down")]))
    with pytest.raises(ToolError, match="GenerationUnavailableError"):
        await tools.answer_question("credit limit customer-991")
