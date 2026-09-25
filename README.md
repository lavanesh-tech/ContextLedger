# ContextLedger

**An MCP-native temporal RAG and decision-provenance platform for AI agents.**

ContextLedger answers one question precisely:

> What exactly did an AI agent know at the moment it made a decision, where did
> that information come from, was it valid and authorized at that time, and
> which later decisions depended on it?

If a customer's credit limit was 2000 (v18) until 14:15 and 5000 (v19) after,
ContextLedger returns v19 for "what is the limit now?" and reconstructs v18 for
"what did the agent know when it decided at 11:00?", deterministically and
tenant-isolated, with the LLM kept out of every correctness decision.

> **Status: Phase 19 of 31 done, plus an additive AI capability (grounded answers,
> LangChain orchestration, evaluation, a decision-investigator agent).** Only what is
> listed under "What works today" exists. Everything else is on the [roadmap](docs/ROADMAP.md).

## What works today

- FastAPI app factory with typed, validated settings (`pydantic-settings`)
- Structured JSON logging with per-request correlation IDs (`X-Correlation-ID`)
- `GET /api/v1/health` liveness and `GET /api/v1/health/ready` readiness (PostgreSQL + migration state), Swagger UI at `/docs`
- Async SQLAlchemy 2 + asyncpg with a per-request session, Alembic migrations (revision `0001` enables pgvector)
- Multi-tenant foundation: organizations, users, memberships with ADMIN / ENGINEER / VIEWER roles, a permission matrix, tenant-bound repositories, and a row-locked "at least one ADMIN" invariant, with cross-tenant attack tests
- Bitemporal fact versions (valid time + transaction time): append-only history, automatic supersession, and PostgreSQL-enforced guarantees (no overlapping validity via an EXCLUDE constraint, an immutability trigger, tenant-safe composite foreign keys)
- Deterministic temporal resolution: current value, value at time T, **what was known at time K**, history, changes between T1 and T2, lineage. A differential test checks the SQL engine against a pure-Python reference on random histories. See [docs/TEMPORAL.md](docs/TEMPORAL.md)
- Provenance: immutable, content-addressed (SHA-256) evidence from each source, linked append-only to the exact fact versions it supports; recorded atomically with the version
- Embeddings: every fact version is embedded by a background worker (PostgreSQL job queue with `SKIP LOCKED`, leases, retries with backoff, per-tenant reuse of identical text) into pgvector with an HNSW cosine index. Offline deterministic provider by default; OpenAI adapter with retries and strict validation, tested without network calls. See [docs/VECTOR_INDEXING.md](docs/VECTOR_INDEXING.md)
- Hybrid temporal retrieval: pgvector similarity + PostgreSQL full-text search in one statement, pre-filtered by tenant, "valid at T as known at K", role-capped privacy scope and metadata; Reciprocal Rank Fusion with an authority × confidence trust factor and a per-result score breakdown; full-text fallback when the embedding provider is down. See [docs/RETRIEVAL.md](docs/RETRIEVAL.md)
- Decision receipts: the retrieved context is frozen at decision time; the decision records which of those facts it relied on (enforced by a foreign key); a canonical SHA-256 receipt hash is re-verified on every read; facts and evidence are shown exactly as known when the decision was made. See [docs/DECISION_RECEIPTS.md](docs/DECISION_RECEIPTS.md)
- Neo4j provenance graph fed by a trigger-written transactional outbox (idempotent, order-independent, rebuildable projection); impact analysis ("which decisions depend on this source / version / evidence?") and decision lineage, tenant-scoped and privacy-aware. See [docs/PROVENANCE_GRAPH.md](docs/PROVENANCE_GRAPH.md)
- MCP server (FastMCP, stdio) with nine tools for agents: search, point-in-time facts, history, capture context, record decision, receipts, impact, lineage and session context. The tenant and user are fixed by configuration, never by the model, with an agent-level privacy ceiling. See [docs/MCP.md](docs/MCP.md)
- REST API for every capability, with RFC 9457 problem details (stable codes and correlation ids), Swagger UI, a committed OpenAPI document and a generated Postman collection. See [docs/API.md](docs/API.md)
- Authentication and authorization: ES256 JWTs; AI agents as OAuth2 clients (client credentials) with scoped tokens, privacy ceilings, and revocation that takes effect before expiry; RBAC re-checked per request; an automatic sweep verifies every tenant route rejects other tenants. See [docs/AUTH.md](docs/AUTH.md)
- Redis for shared short-lived state: a tenant- and privacy-aware retrieval cache invalidated by generation, distributed per-caller rate limiting, `Idempotency-Key` replay for POSTs, single-use OAuth state, and MCP session state, each with an explicit fail-open or fail-closed policy. See [docs/REDIS.md](docs/REDIS.md)
- Kafka domain events from a trigger-written transactional outbox: tenant-keyed topics, a CloudEvents-style envelope with drift-tested JSON Schemas, idempotent consumers (dedup in the same transaction as the effect), bounded retries and dead-letter topics. Consumers maintain a daily activity read model and invalidate the retrieval cache. See [docs/EVENTS.md](docs/EVENTS.md)
- Contradiction detection: a deterministic rule flags a new version that disagrees with a value another source directly observed, in the same transaction as the write, and emits `contradiction.detected`. Both versions are kept, the rules say which one they prefer (authority, confidence, observation time), and people resolve or dismiss it. An optional LLM review suggests conflicts between different properties, validated against the facts it was given. See [docs/CONTRADICTIONS.md](docs/CONTRADICTIONS.md)
- Revocation impact: a fact version found to be wrong is revoked (append-only, with a reason), which removes it from current and "as known now" answers while "as known before the revocation" queries and decision receipts still show it (marked `revoked_at`). Every decision whose frozen context held it is listed, split into relied-on and merely in context, with `fact.revoked` and `decision.impacted` events. See [docs/REVOCATION.md](docs/REVOCATION.md)
- Retrieval evaluation: Recall@K, Precision@K, MRR, nDCG and temporal / authorization correctness on a labelled synthetic dataset (22 cases), comparing hybrid, hybrid without trust weighting, and full-text-only retrieval (`make eval-retrieval`, no model calls). See [evaluation/README.md](evaluation/README.md)
- Grounded LLM answers (`POST …/answers`, MCP `answer_question`): OpenAI Chat Completions behind a provider interface with one deadline, bounded retries and typed errors; versioned prompts; facts retrieved under the caller's tenant, role, privacy ceiling and time constraints; every citation verified by code, and an answer with an invented citation is withheld. Disabled by default. See [docs/AI.md](docs/AI.md)
- LangChain (`langchain-core` only, no LangGraph) orchestrates the prompt → model chains through an adapter over the same provider, so every call keeps the same cost and failure controls
- A versioned grounded-answer evaluation (synthetic dataset, 18 cases): a free deterministic mode that checks what reaches the model, and an opt-in live mode that measures answer quality and cost. See [evaluation/README.md](evaluation/README.md)
- Historical Decision Investigator (`POST …/investigations`, MCP `investigate_decision`): a bounded tool-calling agent with four read-only tools over the existing services, step and tool-call limits, citation checks against ids the tools returned, and an immutable trace of every run
- Web UI (Next.js 15, React 19, strict TypeScript): search with valid-at / as-known-at and score breakdown, entity timelines, decision receipts with integrity and revocation markers, contradictions, revocation impact and grounded answers, all through the REST API (`make frontend-dev`). See [frontend/README.md](frontend/README.md)
- Docker Compose stack: PostgreSQL 17 + pgvector, Redis, Neo4j, Kafka (KRaft), API, embedding worker, graph projector, event relay and event consumers
- Ruff, mypy `--strict`, pytest + pytest-asyncio, PostgreSQL integration tests, GitHub Actions CI with a Postgres service

## Quick start

Requirements: Python 3.12+, Docker Desktop, GNU Make.

```bash
cp .env.example .env          # then change the passwords
make install                  # backend/.venv with dev tools
make up                       # Docker stack + migrations, waits for health checks
make check                    # ruff + mypy + pytest (DB tests use the running Postgres)
make smoke                    # verify API, readiness, pgvector, Redis, Neo4j, Kafka
make run                      # API from source on http://127.0.0.1:8000/docs
make worker-once              # embed one batch of pending fact versions
make graph-once               # project pending changes into Neo4j
make api-docs                 # regenerate docs/api (OpenAPI + Postman)
make bench-vector             # HNSW vs IVFFlat vs exact search (synthetic data)
make bench-retrieval          # hybrid retrieval latency p50/p95/p99 (synthetic data)
make migrate                  # apply migrations from your Mac
make down                     # stop (keeps data)
```

```bash
curl -i http://127.0.0.1:8000/api/v1/health -H 'X-Correlation-ID: demo-1'
```

## Repository layout

```text
backend/            FastAPI service (app/, tests/, Dockerfile, pyproject.toml)
infrastructure/     Docker init scripts now; Terraform / Kubernetes later
benchmarks/         Reproducible measurements -> benchmarks/results/*.json
evaluation/         Retrieval and grounded-answer evaluation datasets, results
frontend/           Web UI: Next.js + React + TypeScript (see frontend/README.md)
docs/               Architecture, decisions, roadmap, benchmarks
scripts/            Developer scripts (local stack smoke test)
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Architecture decisions](docs/DECISIONS.md)
- [Roadmap](docs/ROADMAP.md)
- [Temporal semantics](docs/TEMPORAL.md)
- [Embeddings and vector indexing](docs/VECTOR_INDEXING.md)
- [Hybrid temporal retrieval](docs/RETRIEVAL.md)
- [Decision receipts](docs/DECISION_RECEIPTS.md)
- [Provenance graph](docs/PROVENANCE_GRAPH.md)
- [MCP server](docs/MCP.md)
- [REST API](docs/API.md)
- [Authentication and authorization](docs/AUTH.md)
- [Redis: cache, rate limits, idempotency, short-lived state](docs/REDIS.md)
- [Domain events (Kafka)](docs/EVENTS.md)
- [Contradiction detection](docs/CONTRADICTIONS.md)
- [Revocation impact](docs/REVOCATION.md)
- [AI: grounded answers, LangChain, evaluation, investigator agent](docs/AI.md)
- [Benchmarks methodology](docs/BENCHMARKS.md)

## Measured results

Every number links to a JSON file in `benchmarks/results/` with its commit SHA
and environment. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md) for the full table.

| Metric | Value | Measured |
|---|---|---|
| API Docker image size | 59.5 MB | Phase 1, local macOS arm64 |
| pytest suite | 34 tests, 0.40 s | Phase 1, local macOS arm64 |
| Hybrid retrieval latency, 10K fact versions (synthetic) | p50 22.2 ms, p95 26.3 ms | Phase 8, local macOS arm64 |
| Hybrid retrieval latency, 100K fact versions (synthetic) | p50 56.8 ms, p95 81.4 ms | Phase 8, local macOS arm64 |
