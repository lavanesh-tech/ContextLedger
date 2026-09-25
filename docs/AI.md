# AI: grounded answers, LangChain orchestration, evaluation, investigator agent

This capability was added after Phase 15. It does not replace any roadmap
phase: Phase 16 (contradiction detection) and Phase 18 (retrieval evaluation)
were built separately afterwards. Design decision: ADR-031 in [DECISIONS.md](DECISIONS.md).

The rule throughout: **the model never decides tenant, permission, time,
version or provenance.** Those are decided by the same deterministic services
the REST API and MCP server already use. The model only writes text from what
it was given, and code checks what it cites.

## Configuration

Generation is off by default (`CONTEXTLEDGER_LLM_PROVIDER=disabled`); the
endpoints then return 503 and the MCP tools report it as disabled.

| Setting | Default | Meaning |
|---|---|---|
| `CONTEXTLEDGER_LLM_PROVIDER` | `disabled` | `openai` to enable (needs `CONTEXTLEDGER_OPENAI_API_KEY`) |
| `CONTEXTLEDGER_OPENAI_CHAT_MODEL` | `gpt-4o-mini` | Chat model |
| `CONTEXTLEDGER_LLM_TIMEOUT_SECONDS` | `30` | One deadline for the whole call, retries included |
| `CONTEXTLEDGER_LLM_MAX_RETRIES` | `2` | Retries on 408/409/429/5xx (honours `Retry-After`) |
| `CONTEXTLEDGER_LLM_MAX_OUTPUT_TOKENS` | `800` | Per model call |
| `CONTEXTLEDGER_LLM_ANSWER_PROMPT_VERSION` | `grounded-answer-v2` | Prompt for answers |
| `CONTEXTLEDGER_LLM_AGENT_MAX_STEPS` | `6` | Model calls per investigation |
| `CONTEXTLEDGER_LLM_AGENT_MAX_TOOL_CALLS` | `12` | Tool calls per investigation |

The API key is a `SecretStr`, never logged, and only read from the environment.

## Code map

| Path | What it is |
|---|---|
| `app/ai/providers.py` | `GenerationProvider`, the OpenAI adapter (JSON schema output, tool calling), the scripted fake, typed errors |
| `app/ai/prompts/` | Registry of immutable, fingerprinted prompts: `grounded-answer-v1`, `-v2`, `investigator-v1` |
| `app/ai/grounding.py` | Packs facts as F1..Fn, parses the model's JSON, checks citations |
| `app/ai/orchestration/` | LangChain: `ContextLedgerChatModel` (a `BaseChatModel` over the provider, with `bind_tools`) and the answer chain |
| `app/services/answers.py` | `GroundedAnswerService` |
| `app/ai/agent/tools.py` | The investigator's four read-only LangChain tools |
| `app/ai/agent/investigator.py` | The bounded agent loop |
| `app/services/investigations.py` | Authorization, running the agent, storing the trace |
| `app/evaluation/` | Dataset loader, metrics and the evaluation runner |

## Grounded answers

`POST /api/v1/organizations/{id}/answers` and MCP `answer_question`.

1. Retrieval (`RetrievalService`) applies membership, role, token scopes,
   privacy ceiling, `valid_at` and `known_at`.
2. No authorized facts: status `insufficient_evidence`, and no model call.
3. The facts are labelled F1..Fn with their values, validity periods, source,
   authority and confidence, and rendered into the configured prompt.
4. A LangChain chain (prompt template → `ContextLedgerChatModel`) calls the
   provider with a strict JSON schema: `answer`, `insufficient_evidence`,
   `cited_facts`, `inferences`.
5. Code checks the citations. A label that was not supplied, or an answer with
   no citations, makes the status `ungrounded` and the answer is withheld.
6. The response resolves each citation to its fact version and source (by our
   code, not the model) and reports retrieval and generation metadata: prompt
   version, model, tokens, latency, attempts.

A provider failure is a 503 `generation_unavailable`, never an invented answer.

## Historical Decision Investigator

`POST /api/v1/organizations/{id}/investigations`,
`GET …/investigations/{run_id}`, and MCP `investigate_decision`.

Example question: "Why was decision X approved, and has a fact it relied on
changed since?"

Tools (LangChain `StructuredTool`s built per request):

| Tool | Calls |
|---|---|
| `get_decision_receipt` | `DecisionService.receipt`: the facts as known when the decision was made |
| `get_fact_lineage` | `TemporalService.lineage`: earlier and later versions; whether it was superseded |
| `find_decisions_relying_on` | `DecisionService.decisions_relying_on` |
| `search_facts` | `RetrievalService.search` |

The loop: each step the model either calls tools or returns the final JSON
(`answer`, `insufficient_evidence`, `cited_ids`). Results are fed back as tool
messages.

Safety properties, each covered by tests:

- The tenant context is fixed by the server when the tools are built. No tool
  has an organization or user argument; extra arguments are rejected.
- Arguments are validated (UUIDs, lengths) before any service is called;
  unknown tools are refused. The agent has no SQL or repository access.
- Missing and forbidden records return the same "not found or not accessible".
- Before any model call, the caller needs `facts:read` and `decisions:read`.
- At most `max_steps` model calls and `max_tool_calls` tool calls; running out
  returns `step_limit`, not a guess.
- `cited_ids` must be ids the tools actually returned; otherwise `ungrounded`.
- Every run, including failed ones, is stored in `agent_runs` (append-only,
  enforced by a trigger): requester, agent client, question, status, answer,
  citations, each tool call (validated arguments, ok/error, latency), prompt
  version, model, tokens, latency. Tool outputs are not stored. Only the
  requester can read a run back.

## Evaluation

See [evaluation/README.md](../evaluation/README.md). Two modes over a
versioned, synthetic dataset of 18 cases:

- **Deterministic** (`make eval`, free): runs the real pipeline against
  PostgreSQL with a model that echoes the supplied facts, and measures what
  reaches the model: context correctness, expected facts supplied, forbidden
  values supplied (cross-tenant, above the privacy ceiling, not yet known).
  Recorded results: both prompt versions, 18 cases, context correctness 1.0,
  expected facts supplied 1.0, forbidden values supplied 0.
- **Live** (`make eval-live`, costs money): calls the real model and measures
  answer accuracy, grounded rate, abstention, tokens and estimated cost.
  **No live run has been recorded yet**, so no answer-quality claim is made.

## Tests

No test calls a real model. The OpenAI adapter is tested with a mocked HTTP
transport; everything else uses `FakeGenerationProvider`, which can script
text replies, tool calls and failures. Integration tests run the answer and
investigation endpoints and the MCP tools against real PostgreSQL.

## Limits and next steps

- Answer quality is unmeasured until a live evaluation is recorded.
- No investigator evaluation dataset yet (tool-use correctness, grounded rate).
- No per-tenant token budget for the LLM endpoints yet; the general rate
  limiter applies.
- The agent loop runs tools sequentially and has no checkpointing.
