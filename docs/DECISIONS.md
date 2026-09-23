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

---

## ADR-013: Bitemporal, append-only fact versions

**Status:** Accepted (Phase 4)

**Context.** ContextLedger must answer both "what is true now?" and "what did
the agent know at the moment it decided?", including after later corrections.
Overwriting a value, or storing only one timeline, makes the second question
impossible to answer.

**Decision.**
- **Fact** = identity (entity + property). **FactVersion** = immutable value.
- Two timelines per version:
  - *valid time*, `[valid_from, valid_until)`, half-open, `NULL` = open-ended;
  - *transaction time*, `recorded_at` (+ `valid_until_recorded_at` when the
    version is closed), both from the database clock (`now()`, i.e. the transaction timestamp).
- Supersession rules are pure functions in `app/domain/facts.py`
  (unit-tested without a database): a new version must start after the latest one;
  an open latest version is closed at the new start; fixed windows allow gaps
  but not overlaps. Rewriting history is a **revocation** (Phase 17), not an edit.
- Writes to one fact are serialised with `SELECT ... FOR UPDATE` on the fact row;
  a 10-writer race test proves the result is always one gap-free chain.
- `value` is JSONB (numbers, strings, booleans, arrays, objects; never JSON
  null), capped at 16 KB. `confidence` is `NUMERIC(4,3)`; `authority` 0–100
  defaults from the source.

**Alternatives.** Temporal tables / system versioning (not native in PostgreSQL);
a separate history table (two places to query, easy to forget one); event
sourcing only (every read rebuilds state, harder to index for retrieval).

**Consequences.** Storage grows with every change (intended: history is the
product). Reads need temporal predicates, which is what the Phase 5 engine and
its indexes are for.

---

## ADR-014: Integrity enforced in PostgreSQL, not only in services

**Status:** Accepted (Phase 4)

**Decision.** The rules that make provenance trustworthy are also database constraints:
- `EXCLUDE USING gist` with `btree_gist`: no overlapping validity per fact;
- a `BEFORE UPDATE OR DELETE` trigger: `fact_versions` is append-only, and
  `valid_until` can be set exactly once;
- **composite, tenant-scoped foreign keys**, e.g. `facts(organization_id,
  entity_id) → entities(organization_id, id)`: a row can only reference rows of
  the same organization, even if a bug passes a foreign id;
- `(fact_id, supersedes_id) → fact_versions(fact_id, id)` and
  `UNIQUE(supersedes_id)`: lineage stays within one fact and forms a single chain.

Integration tests bypass the services with raw SQL to prove each one fires.
Objects Alembic cannot model (EXCLUDE constraints, triggers) live only in
migrations and are excluded from drift checks by name prefix (`ex_`).

**Consequences.** A little more migration SQL, in exchange for guarantees that
hold for every writer: services, scripts, manual fixes and future workers.

---

## ADR-015: Temporal resolution: one definition, two implementations, differential tests

**Status:** Accepted (Phase 5)

**Context.** Temporal answers must be exactly right: "what did the agent know at
11:00" is the product's core promise. Bitemporal edge cases (boundaries, open
ends learned later, gaps, late-arriving knowledge) are easy to get subtly wrong
in SQL.

**Decision.**
- The semantics are defined once, as small pure functions
  (`app/temporal/reference.py`), with unit tests that control both timelines
  (including a "late sync" scenario where the true value differs from what was known).
- Production queries use SQL (`valid_at_condition`), so they can later be
  combined with pgvector similarity, full-text search and tenant filters in one statement.
- A **differential test** records seeded random histories (gaps, fixed ends,
  several facts) in PostgreSQL, then compares SQL answers to the reference at
  every boundary, ±1 µs, across both timelines (250 sampled (T, K) pairs).
- `changes_between`, `lineage` and `history` reuse the reference functions over
  the (small, per-entity) set of fetched versions.

**Consequences.** Any future SQL optimisation (indexes, rewritten predicates) is
safe to make: the differential test catches semantic drift.

---

## ADR-016: Transaction time from `clock_timestamp()` under the fact lock

**Status:** Accepted (Phase 5; corrects Phase 4)

**Context.** Phase 4 used `now()` for `recorded_at`. In PostgreSQL `now()` is the
*transaction start* time. Under contention, a writer that started earlier but
waited for the fact lock could record a *later* version with an *earlier*
timestamp, breaking "the versions known at K form a prefix of the chain".

**Decision.** After acquiring the fact's row lock, read `clock_timestamp()` once,
bump it to `previous.recorded_at + 1 µs` if needed (clock steps), and use that
single value for the new version's `recorded_at` and for closing the previous
version (`valid_until_recorded_at`). The 10-writer race test now also asserts
strictly increasing transaction times along the chain.

**Consequences.** Transaction time is monotonic per fact. Across different facts,
timestamps come from the same database clock and remain comparable.

---

## ADR-017: Immutable, content-addressed evidence with append-only links

**Status:** Accepted (Phase 6)

**Context.** A decision receipt is only trustworthy if the material behind each
fact version cannot be silently changed or detached later.

**Decision.**
- `evidence` rows are immutable. Identity within a source is the SHA-256 of the
  normalised excerpt (`\r\n` → `\n`, trimmed), with
  `UNIQUE(organization_id, source_id, content_sha256)`. Capturing the same content
  again returns the existing row (idempotent, race-safe upsert). The first
  capture's metadata wins, and the response says whether a row was created.
- Excerpts are capped at 8,000 characters. Larger documents are stored externally
  (S3 in the AWS phases) and referenced by `uri`; the excerpt holds the relevant passage.
- `fact_version_evidence` links are append-only and carry a relation
  (`SUPPORTS` now; `CONTRADICTS` is used by contradiction detection in Phase 16),
  `linked_at` and `linked_by_user_id`. Re-linking with a different relation is a
  conflict, never an overwrite.
- One reusable trigger function forbids UPDATE/DELETE on provenance tables.
- Composite FKs `(organization_id, fact_version_id)` and
  `(organization_id, evidence_id)` make cross-tenant links impossible, even via raw SQL.
- Evidence may come from a different source than the one that asserted the value
  (corroboration). Evidence has its own `privacy_scope`, which is enforced at retrieval time.

**Consequences.** Removing evidence (legal takedown, mistaken upload) needs an
explicit, audited redaction process instead of DELETE. It is deferred to the
security review (Phase 28) and noted in the roadmap.

---

## ADR-018: Embedding provider abstraction, offline by default

**Status:** Accepted (Phase 7)

**Context.** Embeddings are needed in every environment. Calling a paid API from
unit tests, CI or a laptop without a key would make tests slow, flaky and costly.

**Decision.**
- An `EmbeddingProvider` protocol (`model_id`, `max_batch_size`, `embed`) with two
  implementations: `DeterministicHashEmbeddingProvider` (feature hashing of words
  and word pairs, L2-normalised, 1536 dimensions, offline) and
  `OpenAIEmbeddingProvider`.
- The OpenAI adapter uses `httpx` directly instead of the SDK: a small, fully
  tested surface with explicit retry rules (408/409/429/5xx and transport errors,
  `Retry-After`, jittered exponential backoff), strict response validation, and
  errors that never include the key.
- `deterministic` is the default. Settings validation requires a key when
  `openai` is selected and forbids the deterministic provider in staging and
  production.
- Tests use `httpx.MockTransport`. No test calls the real API.
- `model_id` (`openai:text-embedding-3-small`, `deterministic:hash-v1`) is stored
  with every vector, so vectors from different models are never compared.

**Consequences.** Local retrieval quality with the hash provider reflects word
overlap, not meaning. Quality numbers (Phase 18) must state which provider
produced them.

---

## ADR-019: PostgreSQL job queue for embeddings

**Status:** Accepted (Phase 7)

**Context.** Embedding must not happen inside the fact write transaction: an API
outage would block fact recording. Kafka arrives in Phase 15, and even then a
durable record of "what still needs embedding" is required.

**Decision.**
- `embedding_jobs` table, one row per `(fact_version_id, model)`.
- Claiming uses `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent workers get
  disjoint jobs without a coordinator. A claim sets RUNNING, increments
  `attempts` and issues a fresh lease token.
- The provider is called outside any database transaction. Completion is
  conditional on the lease token, so a worker whose lease expired cannot mark a
  job done. Vectors are written with `ON CONFLICT DO NOTHING` and are never overwritten.
- Leases older than `embedding_lease_seconds` are reclaimable (crash recovery).
  A CHECK constraint ties RUNNING to having a lease.
- Failures go back to PENDING with exponential backoff (`min(cap, base·2^(n-1))`)
  and become FAILED after `embedding_max_attempts`, with the last error kept.
- `enqueue_missing` reconciles from `fact_versions`, so the queue can always be
  rebuilt from the source of truth.
- Per-tenant reuse by content hash avoids paying twice for identical text.

**Consequences.** Polling adds up to `embedding_poll_interval_seconds` of latency
before a new version is searchable. Phase 15 can trigger the worker from Kafka
events. Queue throughput is bounded by PostgreSQL, which is ample at this
project's scale. A dedicated broker would only be justified by measurements.

---

## ADR-020: HNSW cosine index; `ann_` indexes outside autogenerate

**Status:** Accepted (Phase 7)

**Context.** pgvector offers IVFFlat and HNSW. Fact data starts empty and grows
continuously.

**Decision.**
- HNSW with `vector_cosine_ops`, `m = 16`, `ef_construction = 64` (pgvector
  defaults), named `ann_fact_embeddings_embedding_cosine`.
- IVFFlat is rejected as the default: its lists are trained on the rows present
  at build time, so an index built on an empty or small table degrades as data
  grows and needs periodic rebuilds.
- The benchmark (`make bench-vector`, synthetic benchmark dataset) records build
  time, size, recall@10 and latency for exact, IVFFlat and HNSW, so the trade-off
  is measured, not assumed. See [VECTOR_INDEXING.md](VECTOR_INDEXING.md).
- Alembic cannot describe operator classes and index parameters faithfully, so
  indexes prefixed `ann_` (like the existing `ex_` exclusion constraints) are
  created in hand-written migrations and excluded from drift detection.
- The `vector` column type is a small `UserDefinedType` using pgvector's text
  format, so no extra client library is required.

**Consequences.** HNSW builds are slower and use more memory. `hnsw.ef_search`
is the query-time recall/latency knob, to be tuned with Phase 18 and Phase 27
measurements. The first benchmark (static, pre-loaded synthetic data) favoured
IVFFlat on recall and build time. The decision rests on the growing-table case,
which must be measured before it is claimed. If IVFFlat still wins there, this
ADR is revisited.

---

## ADR-021: Hybrid retrieval with pre-filtering and Reciprocal Rank Fusion

**Status:** Accepted (Phase 8)

**Context.** Pure vector search misses exact identifiers and rare words, and pure
keyword search misses paraphrases. Every result must also respect tenant, time
and privacy rules, and those must not depend on the model.

**Decision.**
- Two branches, vector (pgvector cosine, same embedding model only) and full text
  (PostgreSQL `ts_rank_cd`). Both run in one SQL statement (`UNION ALL`) inside a
  REPEATABLE READ, read-only transaction, so they see one snapshot.
- Tenant, valid-at-T-as-known-at-K (Phase 5's `valid_at_condition`), privacy
  scope and metadata filters are **pre-filters** inside each branch. With
  `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8), HNSW keeps scanning
  until enough rows pass them.
- Fusion by Reciprocal Rank Fusion (k = 60), then a transparent trust factor
  `authority/100 × confidence` weighted by `trust_weight` (default 0.3). The
  scoring function is pure and unit tested. Every result carries its score breakdown.
- The query is embedded outside any transaction. A provider failure degrades to
  full text only and is reported in the result (`vector_search="unavailable"`).
- No reranker yet. Phase 18 measures whether one earns its latency and cost.

**Consequences.** RRF ignores score magnitudes: a barely-relevant rank-1 hit
counts like a strong one. The branch candidate pool (4 × limit, 40–200) bounds
the work per query. Latency is measured by `make bench-retrieval`.

---

## ADR-022: Full-text documents maintained by a database trigger

**Status:** Accepted (Phase 8)

**Context.** The searchable text needs the entity type, external id and property,
which live in other tables. A generated column cannot join, and maintaining the
document in application code would miss other write paths (bulk loads,
backfills, future consumers).

**Decision.**
- `fact_search_documents` (one row per version, `tsvector`, GIN index) is filled
  by an `AFTER INSERT` trigger on `fact_versions`. The migration backfills
  existing versions. The table is append-only, like the versions it indexes.
- The text is built by an `IMMUTABLE` SQL function frozen in migration 0006.
  Migrations never import application code.
- Configuration `english`. Questions are parsed with `plainto_tsquery` and their
  terms OR-combined.

**Consequences.** Changing the search text or configuration requires a new
migration that re-creates the documents. Triggers are invisible to Alembic
autogenerate, so they are documented here and covered by integration tests.

---

## ADR-023: Privacy visibility is capped by role and can only be narrowed

**Status:** Accepted (Phase 8)

**Decision.** VIEWER sees PUBLIC and INTERNAL, ENGINEER adds CONFIDENTIAL,
ADMIN adds RESTRICTED. A request may lower the ceiling with `max_privacy_scope`
(for example an external agent limited to PUBLIC) but never raise it. The role is
the one read inside the retrieval transaction, not the caller's cached context.

**Consequences.** Agent-specific scopes (an OAuth client allowed less than its
user) are enforced in Phase 13 by passing a narrower `max_privacy_scope`.

---

## ADR-024: Decision receipts as frozen snapshots sealed by a canonical hash

**Status:** Accepted (Phase 9)

**Context.** An agent's decision has to be explainable later, after the facts
it used have been corrected, superseded or revoked. Re-running retrieval later
answers a different question: what would it decide *now*.

**Decision.**
- Capturing context pins `known_at` (default: database `clock_timestamp()`) and
  stores the ranked retrieval result as an immutable `ContextSnapshot` with
  per-fact score breakdowns. Receipts show facts through the Phase 5
  `as_known(known_at)` rule, and evidence only if it was linked by `known_at`.
- A `Decision` references exactly one snapshot. `DecisionFact` rows carry a
  composite FK to `context_snapshot_facts`, so citing a fact that was not in
  context is impossible even through raw SQL.
- `receipt_sha256` is SHA-256 over canonical JSON (`contextledger.receipt.v1`)
  of the decision, the context and every snapshot fact (id, position, value
  hash, relied-on). It is recomputed on every read and reported as
  `integrity_verified`.
- Readers below a fact's privacy scope see it redacted. Hashing covers the
  unredacted rows, so integrity is verifiable without disclosure.

**Consequences.** Snapshots store references plus scores, not copies of values.
This is sufficient because fact versions are immutable and append-only (Phase 4).
The hash detects edits but not a coordinated rewrite of row and hash.
Signing with an external key (KMS) is deferred to Phase 28.

---

## ADR-025: Neo4j as a projection fed by a trigger-written transactional outbox

**Status:** Accepted (Phase 10)

**Context.** Impact questions ("which decisions depend on this source?") are
variable-depth traversals across facts, versions, evidence, snapshots and
decisions. Writing to two databases from application code (dual writes)
risks the graph silently diverging when one write fails.

**Decision.**
- PostgreSQL remains the only system of record. Neo4j is a rebuildable projection.
- `AFTER INSERT OR UPDATE` triggers on the ten provenance tables write
  `(organization_id, table_name, row_key)` to `graph_outbox` in the same transaction.
- A projector claims events with `FOR UPDATE SKIP LOCKED`, re-reads the current
  rows, writes them in one Neo4j transaction of idempotent `MERGE`s, and deletes
  the events before committing: at-least-once delivery, exactly-once effect.
  Relationships to not-yet-projected nodes create placeholders, so order does not matter.
- The graph stores ids, times, scopes and hashes, never values or excerpts.
- Queries anchor on `{id, org}` after a PostgreSQL permission check, and report
  `pending_events` so eventual consistency is visible to callers.

**Consequences.** Projection lag is roughly the poll interval (2 s by default).
Phase 15 can publish the same outbox to Kafka instead of polling. The outbox
grows if the projector is down, and its size is the obvious alert metric
(Phase 20).

---

## ADR-026: MCP server with a configured principal and an agent privacy ceiling

**Status:** Accepted (Phase 11)

**Context.** Agents call tools with arguments the model produces, and model
output can be influenced by prompt injection. Anything a tool accepts as an
argument must be treated as attacker-controllable.

**Decision.**
- FastMCP (official MCP Python SDK), stdio transport, one process per principal.
- The organization and user come from server configuration. No tool accepts
  them, and a test checks every tool schema for that. Membership is re-resolved
  on every call.
- Each server has an agent name (recorded on decisions) and a maximum privacy
  scope that intersects with the user's role ceiling (ADR-023).
- Tools are thin adapters over the existing services, so REST and MCP cannot
  drift apart. Domain errors map to tool errors. Other exceptions are logged and
  returned as a generic "internal error".
- Writes are two-step: `capture_decision_context` freezes context,
  `record_decision` must cite versions from that snapshot (enforced by the
  database, ADR-024).

**Consequences.** Multi-user remote access needs the streamable HTTP transport
with OAuth (Phase 13). Until then, the MCP server is suitable for local agents
the operator trusts with that user's permissions.
