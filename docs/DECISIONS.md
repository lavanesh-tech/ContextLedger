# Architecture Decision Records

Short records of significant decisions: context, decision, consequences.
New decisions are appended; superseded ones are marked, not deleted.

---

## ADR-001: Python 3.12 + FastAPI + Pydantic v2 for the backend

**Status:** Accepted (Phase 1)

**Context.** The platform is I/O-bound (Postgres, pgvector, OpenAI, Neo4j,
Kafka, Redis) and must expose both REST and MCP. The MCP and OpenAI SDKs are
first-class in Python.

**Decision.** FastAPI on uvicorn, Pydantic v2 for all request/response models,
`pydantic-settings` for configuration, async I/O throughout.

**Consequences.** OpenAPI/Swagger is generated from the same types that
validate requests. Async code needs discipline: no blocking calls on the event loop.

---

## ADR-002: Application factory instead of a module-level `app`

**Status:** Accepted (Phase 1)

**Decision.** `app.main.create_app(settings)` builds the app; uvicorn runs it
with `--factory`. Settings are stored on `app.state` and injected via a dependency.

**Consequences.** Importing the module has no side effects. Tests build isolated
apps with their own settings without patching globals.

---

## ADR-003: Correlation IDs via pure ASGI middleware + ContextVar

**Status:** Accepted (Phase 1)

**Context.** Every log line, and later every trace span, Kafka event and
decision receipt, must be joinable per request. Starlette's
`BaseHTTPMiddleware` has known drawbacks (response buffering, ContextVar
propagation issues, extra task overhead).

**Decision.** A small pure-ASGI middleware reads `X-Correlation-ID`, accepts it
only if it matches `^[A-Za-z0-9._-]{1,128}$` (otherwise mints a UUID4), stores
it in a `ContextVar`, echoes it on the response, and logs one completion record.

**Consequences.** Client-supplied IDs cannot inject newlines or fake JSON into
logs. Known gap: a 500 produced by Starlette's outermost `ServerErrorMiddleware`
does not carry the header; the failure *is* logged with the correlation ID.
Standardized error responses (Phase 12) will close this gap.

---

## ADR-004: Standard-library JSON logging instead of structlog

**Status:** Accepted (Phase 1)

**Decision.** A ~40-line `logging.Formatter` emits one JSON object per line; a
`logging.Filter` attaches the correlation ID. uvicorn's loggers propagate into
the same handler; uvicorn's access log is disabled (`--no-access-log`) because
the middleware logs requests with more context.

**Consequences.** No extra dependency; works with any library that uses
`logging`. Core fields (`timestamp`, `level`, `logger`, `message`,
`correlation_id`) cannot be overwritten by `extra=`. Query strings are not logged.
Secret redaction arrives with the security phases.

---

## ADR-005: Liveness now, readiness in Phase 2

**Status:** Accepted (Phase 1)

**Decision.** `GET /api/v1/health` checks only that the process serves HTTP.
A separate readiness endpoint that checks PostgreSQL arrives in Phase 2.

**Consequences.** An orchestrator will not restart healthy API containers just
because a dependency is briefly unavailable. Readiness gates traffic instead.

---

## ADR-006: PostgreSQL + pgvector, no dedicated vector database

**Status:** Accepted (Phase 1, image choice; schema in Phase 7)

**Context.** Retrieval must combine vector similarity with tenant filters,
temporal validity filters, relational metadata and authorization in one query.

**Decision.** Use the `pgvector/pgvector:pg17` image locally (RDS supports
pgvector in AWS). The extension is enabled by an init script locally and by
Alembic migrations for managed databases.

**Consequences.** One transactional store, one query plan for filters plus
similarity, no cross-system consistency problem. Revisit only if benchmarks
at realistic sizes show pgvector cannot meet latency targets.

---

## ADR-007: Local stack topology

**Status:** Accepted (Phase 1)

**Decision.**
- Kafka 4 in single-node **KRaft** mode (no ZooKeeper), auto topic creation
  disabled so partition counts and keys are chosen deliberately in Phase 15.
- Redis with persistence disabled: it is never the system of record.
- Neo4j Community with a small heap for laptops.
- All published ports bound to `127.0.0.1`; credentials come from `.env`
  (required, no defaults for passwords); the API container only receives the
  `CONTEXTLEDGER_*` variables it needs.

**Consequences.** Local Kafka data is ephemeral (no volume), acceptable because
events can be re-published from PostgreSQL. Replication factor 1 and PLAINTEXT
listeners are development-only settings.

---

## ADR-008: Frontend standard is Next.js + React + TypeScript

**Status:** Accepted (recorded in Phase 1; implemented in Phase 19)

**Context.** ContextLedger needs one web UI for entity timelines, temporal
context reconstruction, RAG search, decision receipts, provenance graphs,
contradictions, revocation impact and audit events. Running two frontend
frameworks would double the build, test and security surface for no benefit.

**Decision.**
- The single frontend framework is **Next.js + React + TypeScript**, in
  `frontend/`. No Angular, and no second frontend framework.
- The frontend is a client of the existing FastAPI **`/api/v1` REST API**. It
  holds no business logic and never connects directly to PostgreSQL, Neo4j,
  Redis or Kafka. Temporal resolution, authorization and provenance stay in the backend.
- TypeScript types for API requests and responses are generated from the
  backend's OpenAPI schema (`/openapi.json`), so a backend contract change
  shows up as a TypeScript compile error.
- Supporting UI libraries (styling, data fetching, graph and chart rendering)
  are chosen inside this standard when Phase 19 starts.

**Consequences.** The backend architecture does not change. Adding the UI later
needs CORS configuration for the frontend origin (secure CORS is part of the
security phases) and a frontend job in CI (lint, type-check, build).
