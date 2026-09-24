import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.core.config import Settings
from app.domain.errors import NotFoundError
from app.domain.facts import PrivacyScope
from app.mcp.serialization import to_jsonable
from app.mcp.server import build_server, identity_from
from app.mcp.tools import McpIdentity, McpRuntime, tool_errors
from app.providers.embeddings import DeterministicHashEmbeddingProvider

ORG, USER = UUID(int=1), UUID(int=2)
EXPECTED_TOOLS = {
    "search_facts",
    "get_entity_facts",
    "get_fact_history",
    "capture_decision_context",
    "record_decision",
    "get_decision_receipt",
    "get_session_context",
    "analyze_impact",
    "get_decision_lineage",
}


def runtime() -> McpRuntime:
    return McpRuntime(
        sessions=None,  # type: ignore[arg-type]  # no tool is invoked
        provider=DeterministicHashEmbeddingProvider(),
        identity=McpIdentity(ORG, USER, "test-agent", PrivacyScope.INTERNAL),
    )


# --- serialisation ------------------------------------------------------------------


@dataclasses.dataclass
class Sample:
    id: UUID
    at: datetime
    amount: Decimal
    scope: PrivacyScope
    tags: frozenset[str]
    nested: dict[str, Any]


def test_to_jsonable_handles_service_types() -> None:
    eastern = datetime(2026, 1, 15, 5, 30, tzinfo=timezone(timedelta(hours=-5)))
    sample = Sample(ORG, eastern, Decimal("0.900"), PrivacyScope.PUBLIC, frozenset({"b", "a"}), {})

    assert to_jsonable(sample) == {
        "id": "00000000-0000-0000-0000-000000000001",
        "at": "2026-01-15T10:30:00+00:00",
        "amount": "0.900",
        "scope": "PUBLIC",
        "tags": ["a", "b"],
        "nested": {},
    }


def test_to_jsonable_rejects_naive_datetimes_and_unknown_types() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_jsonable(datetime(2026, 1, 1))  # noqa: DTZ001 (deliberately naive)
    with pytest.raises(TypeError):
        to_jsonable(object())


# --- server -------------------------------------------------------------------------


async def test_all_tools_are_registered_with_annotations() -> None:
    tools = {tool.name: tool for tool in await build_server(runtime()).list_tools()}

    assert set(tools) == EXPECTED_TOOLS
    assert tools["search_facts"].annotations is not None
    assert tools["search_facts"].annotations.readOnlyHint is True
    assert tools["record_decision"].annotations is not None
    assert tools["record_decision"].annotations.readOnlyHint is False
    assert all(tool.description for tool in tools.values())


async def test_no_tool_lets_the_model_choose_tenant_or_user() -> None:
    for tool in await build_server(runtime()).list_tools():
        properties = set(tool.inputSchema.get("properties", {}))
        assert not properties & {"organization_id", "user_id", "org", "tenant"}, tool.name


async def test_search_facts_schema_bounds_the_limit() -> None:
    tools = {tool.name: tool for tool in await build_server(runtime()).list_tools()}
    limit = tools["search_facts"].inputSchema["properties"]["limit"]

    assert (limit["minimum"], limit["maximum"]) == (1, 50)
    assert "query" in tools["search_facts"].inputSchema["required"]


# --- errors and identity ---------------------------------------------------------------


async def test_domain_errors_become_tool_errors() -> None:
    with pytest.raises(ToolError, match="NotFoundError: decision not found"):
        async with tool_errors("t"):
            raise NotFoundError("decision not found")


async def test_unexpected_errors_are_not_leaked() -> None:
    with pytest.raises(ToolError) as info:
        async with tool_errors("t"):
            raise RuntimeError("password=hunter2 in a stack trace")

    assert "hunter2" not in str(info.value)
    assert "internal error" in str(info.value)


def test_identity_must_be_configured() -> None:
    with pytest.raises(SystemExit, match="MCP_ORGANIZATION_ID"):
        identity_from(Settings(_env_file=None))


def test_identity_from_settings() -> None:
    identity = identity_from(
        Settings(
            _env_file=None,
            mcp_organization_id=ORG,
            mcp_user_id=USER,
            mcp_agent_name="credit-agent",
            mcp_max_privacy_scope="PUBLIC",
        )
    )

    assert identity == McpIdentity(ORG, USER, "credit-agent", PrivacyScope.PUBLIC)
