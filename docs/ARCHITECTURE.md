# Architecture

## The question ContextLedger answers

> What exactly did an AI agent know at the moment it made a decision, where did
> that information come from, was it valid and authorized at that time, and
> which later decisions depended on it?

Example: a customer's `credit_limit` was 2000 (version 18) from 10:30 until
14:15, then became 5000 (version 19). "What is the current limit?" must return
version 19. "What did the agent know when it decided at 11:00?" must
reconstruct version 18, deterministically, without asking an LLM.

## Target architecture

```text
                                          Web UI (P19): Next.js + React + TypeScript
                                                          │  HTTPS, JSON
             AI agents (MCP clients)        Humans / services (REST clients)
                        │                                 │
                  MCP server (P11)                 FastAPI /api/v1
                        └──────────────┬──────────────────┘
                                       │   auth, tenant + agent authorization (P13)
                              Application services  ◄── shared by REST and MCP
     ┌──────────────┬──────────────┬───┴──────────┬───────────────┬──────────────┐
  Temporal        Hybrid RAG     Decision        Provenance      Contradictions /
  resolution      retrieval      receipts        graph           revocation impact
   (P5)            (P8)           (P9)            (P10)           (P16, P17)
     │               │               │               │
 PostgreSQL ◄──── pgvector       PostgreSQL        Neo4j
 (system of record)                                   ▲
     │                                                │
     └──► Kafka events (P15) ──► workers: embeddings, graph projection, impact
                     Redis (P14): cache, rate limits, idempotency, MCP session state
```

**Deterministic core, LLM at the edge.** Tenant authorization, temporal
validity, versioning, revocation and provenance are computed by the backend.
The LLM (OpenAI) only produces embeddings and natural-language explanations of
evidence the backend has already selected and authorized.

**One frontend: Next.js + React + TypeScript.** The web UI (Phase 19) lives
in `frontend/` and is a client of the FastAPI `/api/v1` REST API. It has no
business logic of its own and no direct access to PostgreSQL, Neo4j, Redis or
Kafka. Its TypeScript API types are generated from the backend's OpenAPI
schema, so the UI and the API cannot silently drift apart. See ADR-008.

**PostgreSQL is the source of truth.** Neo4j holds a projection of provenance
relationships for graph traversal; Redis holds disposable state; Kafka carries
change events. Any of them can be rebuilt from PostgreSQL.

## What exists today (Phase 1)

```text
HTTP request
  │
  ▼
uvicorn ──► FastAPI app (built by app.main.create_app)
              │
              ├─ CorrelationIdMiddleware  (pure ASGI)
              │     • reuse a safe X-Correlation-ID or mint a UUID4
              │     • store it in a ContextVar for the whole request
              │     • echo it on the response header
              │     • log one "request.completed" JSON line with status + duration
              │
              └─ /api/v1 router ──► GET /api/v1/health  (liveness only)
```

Local infrastructure (Docker Compose) is running and health-checked but **not
yet used by the API**: PostgreSQL 17 + pgvector, Redis 7.4, Neo4j 5, and Kafka 4
in KRaft mode.

## Backend layout

| Package | Responsibility | Filled in |
|---------|----------------|-----------|
| `app/api` | Thin HTTP routers, dependencies | Phase 1+ |
| `app/core` | Settings, logging, correlation IDs | Phase 1 |
| `app/schemas` | Pydantic request/response models | Phase 1+ |
| `app/models` | SQLAlchemy ORM models | Phase 2+ |
| `app/repositories` | Tenant-scoped data access | Phase 2+ |
| `app/services` | Use cases shared by REST and MCP | Phase 3+ |
| `app/domain` | Framework-free domain rules | Phase 4+ |
| `app/temporal` | Temporal resolution engine | Phase 5 |
| `app/providers` | OpenAI adapters (mocked in tests) | Phase 7 |
| `app/workers` | Embedding and re-index jobs | Phase 7+ |
| `app/retrieval` | Hybrid temporal RAG | Phase 8 |
| `app/provenance` | Neo4j provenance graph | Phase 10 |
| `app/mcp` | MCP server | Phase 11 |
| `app/events` | Kafka schemas, producers, consumers | Phase 15 |
| `app/telemetry` | OpenTelemetry, Prometheus | Phase 20 |

Dependency direction: `api`/`mcp` → `services` → `domain`, `temporal`,
`retrieval`, `repositories` → `models`. Routers never talk to the database directly.
