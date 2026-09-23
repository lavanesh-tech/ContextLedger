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

> **Status: Phase 6 of 31, sources and evidence.** Only what is listed under
> "What works today" exists. Everything else is on the [roadmap](docs/ROADMAP.md).

## What works today

- FastAPI app factory with typed, validated settings (`pydantic-settings`)
- Structured JSON logging with per-request correlation IDs (`X-Correlation-ID`)
- `GET /api/v1/health` liveness and `GET /api/v1/health/ready` readiness (PostgreSQL + migration state), Swagger UI at `/docs`
- Async SQLAlchemy 2 + asyncpg with a per-request session, Alembic migrations (revision `0001` enables pgvector)
- Multi-tenant foundation: organizations, users, memberships with ADMIN / ENGINEER / VIEWER roles, a permission matrix, tenant-bound repositories, and a row-locked "at least one ADMIN" invariant, with cross-tenant attack tests
- Bitemporal fact versions (valid time + transaction time): append-only history, automatic supersession, and PostgreSQL-enforced guarantees (no overlapping validity via an EXCLUDE constraint, an immutability trigger, tenant-safe composite foreign keys)
- Deterministic temporal resolution: current value, value at time T, **what was known at time K**, history, changes between T1 and T2, lineage. A differential test checks the SQL engine against a pure-Python reference on random histories. See [docs/TEMPORAL.md](docs/TEMPORAL.md)
- Provenance: immutable, content-addressed (SHA-256) evidence from each source, linked append-only to the exact fact versions it supports; recorded atomically with the version
- Docker Compose stack: PostgreSQL 17 + pgvector, Redis, Neo4j, Kafka (KRaft)
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
evaluation/         RAG evaluation datasets and runners (Phase 18)
frontend/           Web UI: Next.js + React + TypeScript (Phase 19, not started)
docs/               Architecture, decisions, roadmap, benchmarks
scripts/            Developer scripts (local stack smoke test)
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Architecture decisions](docs/DECISIONS.md)
- [Roadmap](docs/ROADMAP.md)
- [Temporal semantics](docs/TEMPORAL.md)
- [Benchmarks methodology](docs/BENCHMARKS.md)

## Measured results

Every number links to a JSON file in `benchmarks/results/` with its commit SHA
and environment. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md) for the full table.

| Metric | Value | Measured |
|---|---|---|
| API Docker image size | 59.5 MB | Phase 1, local macOS arm64 |
| pytest suite | 34 tests, 0.40 s | Phase 1, local macOS arm64 |
