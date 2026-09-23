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

## What exists today (Phases 1–7)

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
              └─ /api/v1 router
                    ├─ GET /api/v1/health        liveness: process is up (no I/O)
                    └─ GET /api/v1/health/ready  readiness: PostgreSQL reachable
                                                  and migrated? 200 / 503
```

**Database layer (Phase 2).** `create_app()` builds one async SQLAlchemy engine
(asyncpg driver, pooled, `pool_pre_ping`, server-side `statement_timeout`,
`application_name`) and one session factory, stored on `app.state`. Creating the
engine does not connect, so the API starts even if PostgreSQL is down; readiness
reports it. Each request that needs the database gets its own `AsyncSession`
through the `SessionDep` dependency; services own transaction boundaries
(`async with session.begin():`). The lifespan disposes the engine on shutdown.

**Migrations.** Alembic lives in `backend/migrations/` (named so it cannot
shadow the `alembic` package). `env.py` builds the URL from settings, so no
credentials are stored in `alembic.ini`. Revision IDs are sequential
(`0001`, `0002`, ...). Migrations run as a separate one-off step
(`make up`, `make migrate`, or a deploy job), never inside API startup, so several
API replicas never race to migrate. Revision `0001` enables pgvector.

**Multi-tenancy (Phase 3).** Shared database, shared schema, tenant column:
every tenant-owned table has `organization_id NOT NULL REFERENCES organizations
ON DELETE RESTRICT` (`TenantOwnedMixin`). Only `organizations` and `users` are
global, and a unit test fails if any other table lacks the tenant column.

```text
User ──< OrganizationMembership (role: ADMIN | ENGINEER | VIEWER) >── Organization
                                                                          │
                          every later table: organization_id ─────────────┘
```

* **Tenant-bound repositories.** `MembershipRepository(session, organization_id)`
  has no method that accepts another organization id, so a cross-tenant query
  cannot be written through it.
* **TenantContext** (organization, user, role) is resolved from a verified
  membership. "Unknown organization", "not a member" and "inactive user" all
  produce the same error, so organization ids cannot be probed.
* **RBAC.** Code checks permissions (`members:manage`, `facts:write`, ...), never
  role names. The matrix lives in `app/domain/roles.py`.
* **Invariant under concurrency.** Membership changes lock the organization
  row (`SELECT ... FOR UPDATE`) and re-read the actor's *current* role, so two
  simultaneous demotions cannot leave an organization without an ADMIN, and a
  just-revoked role cannot be used from a stale context.

**Temporal facts (Phase 4).**

```text
Entity (customer / customer-991)
  └──< Fact (credit_limit)                 one row per entity + property
         └──< FactVersion                   append-only, never edited
                v1  2000  valid [10:30, 14:15)  recorded 10:31  source: billing
                v2  5000  valid [14:15, ∞)      recorded 14:16  supersedes v1
FactSource (billing DB, CRM API, document, human, agent) ──< FactVersion
```

* **Two timelines per version.** *Valid time* `[valid_from, valid_until)` says
  when the value is true in the world. *Transaction time* (`recorded_at`,
  `valid_until_recorded_at`, set by the database clock) says when ContextLedger
  learned it. Phase 5 uses both to answer "current value" and "what did the agent
  know at 11:00" deterministically.
* **Single write path.** `FactService.record_version` validates input, checks
  `facts:write` against the actor's current role, upserts entity and fact
  race-safely, locks the fact row, plans the new version with pure domain rules
  (`app/domain/facts.py`), closes the previous open version and inserts the new one.
* **Guarantees enforced by PostgreSQL**, not only by application code:
  - `EXCLUDE USING gist (fact_id WITH =, tstzrange(valid_from, valid_until) WITH &&)`:
    versions of one fact never overlap in valid time;
  - a trigger makes `fact_versions` append-only (no DELETE; the only UPDATE is
    closing an open version once);
  - composite foreign keys `(organization_id, entity_id)`, `(organization_id,
    source_id)` and `(fact_id, supersedes_id)` keep every reference inside one
    tenant and lineage inside one fact; `UNIQUE(supersedes_id)` keeps lineage a chain;
  - CHECK constraints for identifiers, ranges, JSON non-null and lineage consistency.

**Temporal resolution (Phase 5).** `TemporalService` answers time questions for
an entity: `facts_at(valid_at, known_at)`, `history`, `changes_between`,
`lineage`, `entity_timeline`. The semantics are defined once, as pure functions
(`app/temporal/reference.py`), and implemented in SQL
(`app/repositories/temporal.py`, `valid_at_condition`) so that later phases can
combine "valid at T as known at K" with vector search and tenant filters in one
query. A seeded differential test compares the two implementations at every
boundary, ±1 µs, in both timelines. Full contract: [TEMPORAL.md](TEMPORAL.md).

**Sources and evidence (Phase 6).**

```text
FactSource ──< Evidence (excerpt, sha256, uri, metadata, captured_at)   immutable
                  │
                  └─[SUPPORTS | CONTRADICTS]─> FactVersion              append-only link
```

`EvidenceService.capture_evidence` stores material content-addressed by the
SHA-256 of its normalised text, so re-ingesting the same content is idempotent
(race-safe `ON CONFLICT DO NOTHING`). `RecordFactVersion.evidence_ids` links
evidence in the same transaction as the version: an unknown or foreign id rolls
back the version too. `EvidenceService.provenance(version)` returns the version,
its asserting source and every linked piece of evidence with who linked it and
when. A shared trigger function (`contextledger_forbid_modification`) makes both
tables append-only, and composite foreign keys keep links inside one tenant.
Phase 10 projects exactly these rows into the Neo4j provenance graph.

**Embeddings (Phase 7).**

```text
FactVersion ──(embedding_jobs: PENDING → RUNNING → SUCCEEDED | FAILED)──► worker
                                                                            │ render "fact-text-v1"
                                                                            │ reuse tenant vector by SHA-256
                                                                            │ provider.embed (outside transactions)
                                                                            ▼
                                         fact_embeddings (vector(1536), HNSW cosine index)
```

A separate worker process (`python -m app.workers.embeddings`, Compose service
`worker`) claims jobs with `FOR UPDATE SKIP LOCKED` and lease tokens, retries
with exponential backoff, and reclaims jobs from crashed workers. The provider
is the offline deterministic one by default and OpenAI in staging and
production. `EmbeddingRepository.nearest` is the tenant-scoped cosine search
that Phase 8 combines with temporal filters. Details and the HNSW vs IVFFlat
benchmark: [VECTOR_INDEXING.md](VECTOR_INDEXING.md).

Still running but not yet used by the API: Redis 7.4, Neo4j 5, Kafka 4 (KRaft).

## Backend layout

| Package | Responsibility | Filled in |
|---------|----------------|-----------|
| `app/api` | Thin HTTP routers, dependencies | Phase 1+ |
| `app/core` | Settings, logging, correlation IDs | Phase 1 |
| `app/schemas` | Pydantic request/response models | Phase 1+ |
| `app/db` | Declarative base, engine, session factory, migration helpers | Phase 2 |
| `app/models` | ORM models: tenants/users/memberships, entities, sources, facts, fact versions | Phase 3+ |
| `app/repositories` | Tenant-scoped data access | Phase 3+ |
| `app/services` | Use cases shared by REST and MCP (readiness since Phase 2) | Phase 2+ |
| `app/domain` | Framework-free rules: roles/permissions, tenant context, validation, errors | Phase 3+ |
| `app/temporal` | Bitemporal value types and reference semantics | Phase 5 |
| `app/providers` | Embedding providers: deterministic (offline) and OpenAI (mocked in tests) | Phase 7 |
| `app/workers` | Embedding worker (Postgres job queue) | Phase 7 |
| `app/retrieval` | Hybrid temporal RAG | Phase 8 |
| `app/provenance` | Neo4j provenance graph | Phase 10 |
| `app/mcp` | MCP server | Phase 11 |
| `app/events` | Kafka schemas, producers, consumers | Phase 15 |
| `app/telemetry` | OpenTelemetry, Prometheus | Phase 20 |

Dependency direction: `api`/`mcp` → `services` → `domain`, `temporal`,
`retrieval`, `repositories` → `models`. Routers never talk to the database directly.
