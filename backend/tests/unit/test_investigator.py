"""Historical Decision Investigator: tool calling, bounds, grounding and tenant safety.

The model is scripted (FakeGenerationProvider); the backend is an in-memory fake.
No network, no API key, no database.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.ai.agent.investigator import (
    DecisionInvestigator,
    InvestigationError,
    InvestigationStatus,
)
from app.ai.agent.tools import NOT_ACCESSIBLE, ToolLedger, build_tools
from app.ai.orchestration.chat_model import ContextLedgerChatModel, to_chat_messages
from app.ai.providers import (
    ChatMessage,
    FakeGenerationProvider,
    GenerationRequest,
    GenerationUnavailableError,
    OpenAIChatProvider,
    ToolCall,
    ToolSpec,
)
from app.domain.errors import NotFoundError, PermissionDeniedError
from app.domain.facts import PrivacyScope
from app.domain.roles import MembershipRole
from app.domain.tenancy import TenantContext
from app.services.decisions import DecisionReceipt, ReceiptContext, ReceiptFact
from app.services.retrieval import RetrievalQuery, RetrievalResult
from app.temporal.model import Lineage, VersionSnapshot

T0 = datetime(2026, 1, 15, 9, tzinfo=UTC)
CTX = TenantContext(organization_id=UUID(int=1), user_id=UUID(int=2), role=MembershipRole.ENGINEER)
DECISION = UUID(int=500)
V1 = UUID(int=11)
V2 = UUID(int=12)


def _snapshot(vid: UUID, n: int, value: Any, hours: int) -> VersionSnapshot:
    return VersionSnapshot(
        id=vid,
        fact_id=UUID(int=99),
        version=n,
        value=value,
        source_id=UUID(int=50),
        valid_from=T0 + timedelta(hours=hours),
        valid_until=None,
        observed_at=T0,
        recorded_at=T0 + timedelta(hours=hours),
        valid_until_recorded_at=None,
        supersedes_id=None,
        authority=90,
        confidence=Decimal("1.000"),
        privacy_scope=PrivacyScope.INTERNAL,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TenantContext, Any]] = []

    async def receipt(self, ctx: TenantContext, decision_id: UUID) -> DecisionReceipt:
        self.calls.append(("receipt", ctx, decision_id))
        if decision_id != DECISION:
            raise NotFoundError("decision not found")
        fact = ReceiptFact(
            fact_version_id=V1,
            position=0,
            relied_on=True,
            redacted=False,
            privacy_scope=PrivacyScope.INTERNAL,
            entity_type="customer",
            external_id="customer-991",
            property="credit_limit",
            source_name="billing-db",
            version=_snapshot(V1, 1, {"amount": 5000}, 0),
            ranking={},
            evidence=(),
        )
        return DecisionReceipt(
            decision_id=DECISION,
            organization_id=ctx.organization_id,
            action="credit.approve_increase",
            outcome={"approved": True},
            rationale="limit was 5000",
            agent="credit-agent",
            decided_by_user_id=ctx.user_id,
            decided_at=T0 + timedelta(hours=1),
            context=ReceiptContext(
                snapshot_id=UUID(int=600),
                query="credit limit",
                valid_at=T0 + timedelta(hours=1),
                known_at=T0 + timedelta(hours=1),
                embedding_model="fake",
                vector_search="used",
                privacy_scopes=("internal",),
                parameters={},
                captured_by_user_id=ctx.user_id,
            ),
            facts=(fact,),
            receipt_sha256="0" * 64,
            integrity_verified=True,
        )

    async def lineage(self, ctx: TenantContext, version_id: UUID) -> Lineage:
        self.calls.append(("lineage", ctx, version_id))
        if version_id != V1:
            raise PermissionDeniedError("role lacks permission")
        return Lineage(
            version=_snapshot(V1, 1, {"amount": 5000}, 0),
            ancestors=(),
            descendants=(_snapshot(V2, 2, {"amount": 2000}, 48),),
        )

    async def decisions_relying_on(self, ctx: TenantContext, version_id: UUID) -> list[UUID]:
        self.calls.append(("relying", ctx, version_id))
        return [DECISION]

    async def search(self, ctx: TenantContext, query: RetrievalQuery) -> RetrievalResult:
        self.calls.append(("search", ctx, query))
        return RetrievalResult(
            query=query.query,
            valid_at=T0,
            known_at=None,
            vector_search="unavailable",
            embedding_model="fake",
            privacy_scopes=(PrivacyScope.INTERNAL,),
            vector_candidates=0,
            text_candidates=0,
        )


def call(name: str, i: int = 1, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{i}", name=name, arguments=args)


def final(answer: str, cited: list[str], insufficient: bool = False) -> str:
    return json.dumps({"answer": answer, "insufficient_evidence": insufficient, "cited_ids": cited})


async def test_investigator_follows_tools_and_cites_only_observed_ids() -> None:
    backend = FakeBackend()
    fake = FakeGenerationProvider(
        responses=[
            [call("get_decision_receipt", 1, decision_id=str(DECISION))],
            [call("get_fact_lineage", 2, fact_version_id=str(V1))],
            final("Approved on a 5000 limit that later fell to 2000.", [str(DECISION), str(V2)]),
        ]
    )

    result = await DecisionInvestigator(backend, fake).investigate(CTX, "Why was it approved?")

    assert result.status is InvestigationStatus.ANSWERED
    assert result.cited_ids == [str(DECISION), str(V2)] and result.rejected_ids == []
    assert result.steps == 3 and [r.tool for r in result.tool_calls] == [
        "get_decision_receipt",
        "get_fact_lineage",
    ]
    assert all(c[1] is CTX for c in backend.calls)  # the server's tenant, always
    # Every request advertised the tools and the final-answer schema.
    assert [t.name for t in fake.requests[0].tools][:2] == [
        "get_decision_receipt",
        "get_fact_lineage",
    ]
    assert fake.requests[0].output_schema is not None
    # The tool result was fed back with the id of the call it answers.
    tool_msg = fake.requests[1].messages[-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "call_1"
    assert json.loads(tool_msg.content)["facts"][0]["value"] == {"amount": 5000}


async def test_an_invented_id_makes_the_result_ungrounded() -> None:
    fake = FakeGenerationProvider(
        responses=[
            [call("get_decision_receipt", decision_id=str(DECISION))],
            final("It relied on fact X.", [str(DECISION), str(UUID(int=777))]),
        ]
    )
    result = await DecisionInvestigator(FakeBackend(), fake).investigate(CTX, "why?")
    assert result.status is InvestigationStatus.UNGROUNDED
    assert result.answer is None and result.rejected_ids == [str(UUID(int=777))]


async def test_an_answer_with_no_citations_is_ungrounded() -> None:
    fake = FakeGenerationProvider(responses=[final("Because reasons.", [])])
    result = await DecisionInvestigator(FakeBackend(), fake).investigate(CTX, "why?")
    assert result.status is InvestigationStatus.UNGROUNDED and result.answer is None


async def test_insufficient_evidence_is_reported() -> None:
    fake = FakeGenerationProvider(responses=[final("No such decision is visible.", [], True)])
    result = await DecisionInvestigator(FakeBackend(), fake).investigate(CTX, "why?")
    assert result.status is InvestigationStatus.INSUFFICIENT_EVIDENCE


async def test_missing_and_forbidden_look_the_same_to_the_model() -> None:
    fake = FakeGenerationProvider(
        responses=[
            [
                call("get_decision_receipt", 1, decision_id=str(UUID(int=9))),
                call("get_fact_lineage", 2, fact_version_id=str(UUID(int=8))),
            ],
            final("Not visible.", [], True),
        ]
    )
    result = await DecisionInvestigator(FakeBackend(), fake).investigate(CTX, "why?")
    outputs = [m.content for m in fake.requests[1].messages if m.role == "tool"]
    assert outputs == [json.dumps({"error": NOT_ACCESSIBLE})] * 2
    assert [r.ok for r in result.tool_calls] == [False, False]


async def test_the_model_cannot_pass_an_organization_or_bad_arguments() -> None:
    backend = FakeBackend()
    fake = FakeGenerationProvider(
        responses=[
            [
                call(
                    "get_decision_receipt",
                    1,
                    decision_id=str(DECISION),
                    organization_id=str(UUID(int=666)),
                ),
                call("get_fact_lineage", 2, fact_version_id="not-a-uuid"),
                call("run_sql", 3, sql="select * from facts"),
            ],
            final("Nothing.", [], True),
        ]
    )
    await DecisionInvestigator(backend, fake).investigate(CTX, "why?")
    tool_messages = [m for m in fake.requests[1].messages if m.role == "tool"]
    outputs = [json.loads(m.content)["error"] for m in tool_messages]
    assert outputs[0].startswith("invalid arguments") and "organization_id" in outputs[0]
    assert outputs[1] == "invalid arguments: fact_version_id"
    assert outputs[2] == "unknown tool 'run_sql'"
    assert backend.calls == []  # nothing reached a service


async def test_step_limit_stops_the_loop_without_an_answer() -> None:
    fake = FakeGenerationProvider(
        responder=lambda _: [call("find_decisions_relying_on", fact_version_id=str(V1))]
    )
    result = await DecisionInvestigator(FakeBackend(), fake, max_steps=3).investigate(CTX, "?")
    assert result.status is InvestigationStatus.STEP_LIMIT and result.answer is None
    assert len(fake.requests) == 3


async def test_tool_call_budget_is_enforced() -> None:
    backend = FakeBackend()
    many = [call("find_decisions_relying_on", i, fact_version_id=str(V1)) for i in range(5)]
    fake = FakeGenerationProvider(responses=[many, final("done", [str(DECISION)])])
    await DecisionInvestigator(backend, fake, max_tool_calls=2).investigate(CTX, "?")
    assert len(backend.calls) == 2
    outputs = [m.content for m in fake.requests[1].messages if m.role == "tool"]
    assert outputs[2:] == [json.dumps({"error": "tool call budget exhausted"})] * 3


async def test_provider_failure_raises_instead_of_guessing() -> None:
    fake = FakeGenerationProvider(responses=[GenerationUnavailableError("down")])
    with pytest.raises(InvestigationError) as info:
        await DecisionInvestigator(FakeBackend(), fake).investigate(CTX, "why?")
    assert info.value.cause == "GenerationUnavailableError"


async def test_tool_schemas_expose_no_tenant_arguments() -> None:
    tools = build_tools(CTX, FakeBackend(), ToolLedger())
    model = ContextLedgerChatModel(provider=FakeGenerationProvider(responses=["x"]))
    for tool in tools:
        props = set(tool.args)
        assert not props & {"organization_id", "user_id", "role", "scopes"}
    assert await model.bind_tools(tools).ainvoke("q")


def test_tool_messages_round_trip_through_the_adapter() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    ai = AIMessage("", tool_calls=[{"id": "c1", "name": "t", "args": {"a": 1}}])
    messages = to_chat_messages([ai, ToolMessage("out", tool_call_id="c1")])
    assert messages[0].tool_calls == (ToolCall("c1", "t", {"a": 1}),)
    assert messages[1] == ChatMessage("tool", "out", tool_call_id="c1")


async def test_openai_provider_sends_tools_and_parses_tool_calls() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": '{"a": 1}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test/v1/"
    ) as client:
        provider = OpenAIChatProvider(client, api_key=SecretStr("sk-test"), model="gpt-4o-mini")
        result = await provider.generate(
            GenerationRequest(
                messages=[
                    ChatMessage("user", "q"),
                    ChatMessage("assistant", "", tool_calls=(ToolCall("c0", "t", {}),)),
                    ChatMessage("tool", "{}", tool_call_id="c0"),
                ],
                tools=(ToolSpec("t", "a tool", {"type": "object", "properties": {}}),),
            )
        )
    assert result.tool_calls == (ToolCall("c1", "t", {"a": 1}),)
    assert seen["tools"][0]["function"]["name"] == "t"
    assert seen["messages"][1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert seen["messages"][2] == {"role": "tool", "content": "{}", "tool_call_id": "c0"}
