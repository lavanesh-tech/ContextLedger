"""ContextLedger MCP server (stdio transport).

Run it as the configured principal:

    CONTEXTLEDGER_MCP_ORGANIZATION_ID=... CONTEXTLEDGER_MCP_USER_ID=... \\
        python -m app.mcp.server

An MCP client (Claude Desktop, an agent framework, the MCP Inspector) starts
this process and talks JSON-RPC over stdin/stdout. Logs go to stderr, never
stdout, so they cannot corrupt the protocol stream.
"""

import asyncio
import logging
import sys
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.cache.retrieval import RetrievalCache
from app.cache.store import build_store
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_session_factory
from app.domain.facts import PrivacyScope
from app.mcp.state import McpSessionState
from app.mcp.tools import McpIdentity, McpRuntime, ToolHandlers
from app.provenance.graph import GraphReader, build_driver
from app.providers.embeddings import build_embedding_provider, build_openai_http_client

logger = logging.getLogger("contextledger.mcp")

INSTRUCTIONS = """\
ContextLedger is the system of record for facts your decisions depend on.
- Use search_facts / get_entity_facts to look facts up. Every fact is valid for a time
  range and was recorded at a known time; pass valid_at / known_at to ask about the past.
- Before acting on facts, call capture_decision_context, then record_decision citing the
  fact_version_ids you relied on (snapshot_id defaults to the one you captured last).
  The returned receipt proves what you knew and when.
- get_session_context shows what this session remembers (it expires after inactivity).
- Use analyze_impact to see which decisions depend on a fact, source or evidence.
You cannot choose the organization or user: they are fixed by the server's configuration.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def build_server(runtime: McpRuntime) -> FastMCP:
    server = FastMCP("ContextLedger", instructions=INSTRUCTIONS)
    tools = ToolHandlers(runtime)
    for handler, annotations in (
        (tools.search_facts, READ_ONLY),
        (tools.get_entity_facts, READ_ONLY),
        (tools.get_fact_history, READ_ONLY),
        (tools.capture_decision_context, WRITES),
        (tools.record_decision, WRITES),
        (tools.get_decision_receipt, READ_ONLY),
        (tools.get_session_context, READ_ONLY),
        (tools.analyze_impact, READ_ONLY),
        (tools.get_decision_lineage, READ_ONLY),
    ):
        server.add_tool(handler, name=handler.__name__, annotations=annotations)
    return server


def identity_from(settings: Settings) -> McpIdentity:
    if settings.mcp_organization_id is None or settings.mcp_user_id is None:
        raise SystemExit(
            "Set CONTEXTLEDGER_MCP_ORGANIZATION_ID and CONTEXTLEDGER_MCP_USER_ID "
            "(the organization and user this MCP server acts as)."
        )
    return McpIdentity(
        organization_id=settings.mcp_organization_id,
        user_id=settings.mcp_user_id,
        agent_name=settings.mcp_agent_name,
        max_privacy_scope=PrivacyScope(settings.mcp_max_privacy_scope),
    )


async def _main(settings: Settings) -> None:
    identity = identity_from(settings)
    engine = create_engine(settings)
    http_client = (
        build_openai_http_client(settings) if settings.embedding_provider == "openai" else None
    )
    driver = build_driver(settings) if settings.neo4j_password.get_secret_value() else None
    store = build_store(settings)
    # One stdio process serves one client session.
    session_id = uuid.uuid4().hex
    try:
        runtime = McpRuntime(
            sessions=create_session_factory(engine),
            provider=build_embedding_provider(settings, http_client),
            identity=identity,
            graph=None if driver is None else GraphReader(driver, settings.neo4j_database),
            cache=RetrievalCache(store, ttl_seconds=settings.retrieval_cache_ttl_seconds),
            state=McpSessionState(
                store,
                organization_id=identity.organization_id,
                user_id=identity.user_id,
                agent_name=identity.agent_name,
                session_id=session_id,
                ttl_seconds=settings.mcp_session_ttl_seconds,
            ),
        )
        logger.info(
            "mcp.server_started",
            extra={"organization_id": str(identity.organization_id), "agent": identity.agent_name},
        )
        await build_server(runtime).run_stdio_async()
    finally:
        await store.close()
        if driver is not None:
            await driver.close()
        if http_client is not None:
            await http_client.aclose()
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    asyncio.run(_main(settings))


if __name__ == "__main__":
    main()
