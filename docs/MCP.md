# MCP Server

ContextLedger exposes its capabilities to AI agents through the
[Model Context Protocol](https://modelcontextprotocol.io). The server is
`app/mcp/server.py` (FastMCP, stdio transport). Every tool is a thin adapter
over the same services the REST API will use, so the rules are identical.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `search_facts` | read | Hybrid (vector + full-text) search, valid at `valid_at`, as known at `known_at` |
| `get_entity_facts` | read | Every fact of one entity at a point in time; reports how many were withheld by privacy scope |
| `get_fact_history` | read | Every version of every fact of an entity, as known at `known_at` |
| `capture_decision_context` | write | Retrieve **and freeze** the context for a decision (returns `snapshot_id`) |
| `record_decision` | write | Record the decision, citing `relied_on` fact versions from the snapshot (`snapshot_id` defaults to the session's last capture); returns a sealed receipt |
| `get_decision_receipt` | read | The full receipt, with `integrity_verified` |
| `analyze_impact` | read | Decisions depending on a fact version, source or evidence (Neo4j) |
| `get_decision_lineage` | read | What a decision rests on: versions, sources, evidence (Neo4j) |
| `get_session_context` | read | What this session remembers: last snapshot, its fact versions, recent queries (Redis, expires after inactivity) |
| `answer_question` | read, calls an LLM | A grounded answer from facts this agent may see, with verified citations ([AI.md](AI.md)) |
| `investigate_decision` | stores a trace, calls an LLM | The decision-investigator agent; the run is stored in `agent_runs` ([AI.md](AI.md)) |

The two AI tools fold the agent's privacy ceiling into the tenant context, so
the model behind them sees only what this agent may see. They report
"LLM generation is disabled" unless `CONTEXTLEDGER_LLM_PROVIDER=openai`.

`search_facts` shares the retrieval cache with the REST API and reports
`cache: hit | miss`. Session state is convenience only; see [REDIS.md](REDIS.md).

Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`) so clients can
ask for confirmation before writes.

## Security model

* **The principal is configuration, not input.** `CONTEXTLEDGER_MCP_ORGANIZATION_ID`
  and `CONTEXTLEDGER_MCP_USER_ID` fix who the server acts as. No tool has a tenant
  or user argument (a unit test checks every schema), so prompt injection cannot
  switch organizations.
* **Membership is re-checked on every call.** Removing the user from the
  organization takes effect on the next tool call. A test covers this.
* **Agents get their own privacy ceiling.** `CONTEXTLEDGER_MCP_MAX_PRIVACY_SCOPE`
  (default `INTERNAL`) narrows what the user's role allows, and never widens it.
* **Roles still apply.** A VIEWER's agent can read but cannot record decisions.
* **Errors are safe.** Domain errors become readable tool errors (e.g.
  `ValidationFailedError: ...`). Unexpected errors are logged server-side and
  shown to the agent only as "internal error".
* **stdout is the protocol.** Logs go to stderr so they cannot corrupt JSON-RPC.

Remote agents over HTTP, authenticated with OAuth 2.1 per the MCP authorization
spec, arrive in Phase 13. The stdio server is for a local, trusted process.

## Running it

```bash
make up
CONTEXTLEDGER_MCP_ORGANIZATION_ID=<org uuid> CONTEXTLEDGER_MCP_USER_ID=<user uuid> make mcp
```

Example client configuration (e.g. Claude Desktop's `claude_desktop_config.json`),
with paths adjusted to your checkout:

```json
{
  "mcpServers": {
    "contextledger": {
      "command": "/path/to/ContextLedger/backend/.venv/bin/python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/path/to/ContextLedger/backend",
      "env": {
        "CONTEXTLEDGER_MCP_ORGANIZATION_ID": "<org uuid>",
        "CONTEXTLEDGER_MCP_USER_ID": "<user uuid>",
        "CONTEXTLEDGER_MCP_AGENT_NAME": "desktop-agent"
      }
    }
  }
}
```

A typical agent flow: `search_facts` → `capture_decision_context` → decide →
`record_decision` (citing `fact_version_id`s) → later, `get_decision_receipt` or
`analyze_impact` when a fact turns out to be wrong.
