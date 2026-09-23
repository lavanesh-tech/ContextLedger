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

---

## ADR-009: Async SQLAlchemy 2 + asyncpg, one engine per process, session per request

**Status:** Accepted (Phase 2)

**Context.** The API is async end to end. Blocking database calls on the event
loop would stall every concurrent request.

**Decision.**
- SQLAlchemy 2.x async ORM with the **asyncpg** driver.
- `create_app()` builds **one engine per process** (connection pool) and one
  `async_sessionmaker`, stored on `app.state`; the lifespan disposes the engine.
- Pool: `pool_pre_ping=True` (survives DB restarts/failovers), bounded
  `pool_size`/`max_overflow`/`pool_timeout`, `pool_recycle`.
- Server-side `statement_timeout` (30 s default) and `application_name` are set
  per connection, so a runaway query cannot hold a pool slot forever and
  connections are identifiable in `pg_stat_activity`.
- **One `AsyncSession` per request** via the `SessionDep` dependency.
  **Services own transactions** (`async with session.begin():`); routers never commit.
- `expire_on_commit=False`, because implicit lazy loads after commit are illegal I/O in async code.
- DB settings are separate fields (`CONTEXTLEDGER_DB_HOST`, `..._PASSWORD`, ...)
  combined with `URL.create()`, which escapes special characters; they map
  directly onto an AWS RDS Secrets Manager secret. The password is a `SecretStr`
  and is **required** in staging and production.
- Every `Mapped[datetime]` column is `TIMESTAMP WITH TIME ZONE`, and constraint
  names follow a fixed naming convention so migrations can alter them later.

**Consequences.** Creating the engine does not connect, so the API can start
while PostgreSQL is down, and readiness reports it. Pool sizes are defaults for
now and will be tuned with load-test measurements (Phase 27).

---

## ADR-010: Alembic migrations as a separate step; readiness semantics

**Status:** Accepted (Phase 2)

**Decision.**
- Alembic lives in `backend/migrations/` (not `backend/alembic/`, which would
  shadow the installed `alembic` package for Python and mypy).
- `env.py` gets the URL from application settings. No credentials in `alembic.ini`.
- Sequential revision IDs (`0001`, `0002`, ...). A unit test enforces one head, a
  linear history and a `downgrade()` in every migration.
- **Migrations never run inside API startup.** They run as a one-off step
  (`make up` / `make migrate` locally, a deploy job in AWS later). This avoids
  several replicas racing to migrate and keeps a failed migration from crash-looping the API.
- Migrations ship inside the API image, so the same artifact runs them.
- CI runs `alembic upgrade head`, `alembic check` (models vs migrations drift),
  a full `downgrade base` / `upgrade head` round trip, then the tests.
- **Readiness** (`GET /api/v1/health/ready`) returns 503 when PostgreSQL is
  unreachable or has never been migrated. A revision *mismatch* is reported
  (`up_to_date: false`) but still counts as ready, because expand/contract
  migrations keep the previous release compatible during rolling deploys.
  Errors expose only the exception type, never connection details.

**Consequences.** Deployments need an explicit "migrate" step before rolling
out new API instances, which is standard practice and will be modelled in the AWS phases.

---

## ADR-011: Multi-tenancy by shared schema + `organization_id`, enforced in three layers

**Status:** Accepted (Phase 3)

**Context.** Every fact, decision and embedding belongs to exactly one
organization. A leak across tenants is the most serious failure this product can have.

**Options considered.** Database-per-tenant (strong isolation, expensive and
slow to operate), schema-per-tenant (migrations multiply per tenant), shared
schema with a tenant column (cheap, simple, but only safe if enforced everywhere).

**Decision.** Shared schema with a tenant column, enforced in three layers:
1. **Database.** `organization_id NOT NULL` + FK `ON DELETE RESTRICT` on every
   tenant-owned table, plus CHECK/UNIQUE constraints. A unit test fails if a new
   table is neither tenant-owned nor explicitly listed in `GLOBAL_TABLES`.
2. **Repositories.** Tenant-owned repositories are constructed with one
   `organization_id` and expose no way to query another.
3. **Services.** Every call takes a `TenantContext`. Mutations re-verify the
   actor's membership inside the transaction.

Cross-tenant attempts are tested explicitly (resolving another org, listing,
changing, removing, forged contexts). "Not found" and "not a member" responses
do not reveal whether another tenant's ids exist.

**Consequences.** Composite indexes must lead with `organization_id`.
PostgreSQL Row-Level Security is a candidate fourth layer for the security phases.

---

## ADR-012: RBAC via a permission matrix; last-ADMIN invariant under row lock

**Status:** Accepted (Phase 3)

**Decision.**
- Roles live on the **membership** (per organization), not on the user.
- Roles: `ADMIN ⊃ ENGINEER ⊃ VIEWER` (strict supersets, unit-tested).
  Code checks **permissions** (`facts:write`, `members:manage`, ...), so the
  matrix can change in one file.
- Roles are stored as `VARCHAR + CHECK` (via a `StringEnum` column type), not a
  native PostgreSQL ENUM, because adding a value later is an ordinary migration.
- **Invariant: an organization always has at least one ADMIN.** Creating an
  organization writes the org and its first ADMIN membership in one transaction.
  Demote/remove operations take `SELECT ... FOR UPDATE` on the organization
  row, then re-read the actor's current role and count admins. An integration
  test runs two concurrent cross-demotions on separate connections and proves
  exactly one succeeds.
- Users have no password column. Identity comes from OAuth2/JWT in Phase 13;
  the TenantContext will then be built from the verified token.

**Consequences.** Membership changes within one organization are serialised
(low volume, so there is no throughput concern). Reads are not locked.
