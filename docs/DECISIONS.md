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

---

## ADR-027: REST conventions: problem details, one principal dependency, dataclass read models

**Status:** Accepted (Phase 12)

**Decision.**
- Every error is RFC 9457 `application/problem+json` with a stable `code` and
  the request's `correlation_id`. Domain errors map by type (NotFound 404,
  PermissionDenied 403, Conflict/InvariantViolation 409, ValidationFailed 422).
  Unexpected exceptions are answered inside the correlation middleware as a
  generic 500 that still carries the correlation id.
- Identity comes from a single dependency, `get_principal`. Phase 12 ships
  `development-headers` (refused in staging and production by settings
  validation), and Phase 13 swaps in JWT without touching the endpoints.
- Tenant resolution (`TenantDep`) returns the same 403 for unknown
  organizations and non-members, so organization ids cannot be probed.
- Requests are strict pydantic models (`extra="forbid"`, timezone-aware
  datetimes). ORM rows are mapped through explicit `*Out` models. Read models
  that already exist as frozen service dataclasses (receipts, search results,
  impact reports) are returned directly, so REST, MCP and the services share
  one definition.
- The OpenAPI document and a generated Postman collection are committed, and a
  test fails when they drift from the code.

**Consequences.** Returning service dataclasses couples the wire format to
service types. A breaking change to one is a breaking change to the API, which
the committed OpenAPI diff makes visible in review.

---

## ADR-028: Self-issued ES256 JWTs; agents as OAuth2 clients backed by service users

**Status:** Accepted (Phase 13)

**Context.** Agents need machine credentials with less power than a person,
revocable instantly, and they must not bypass the RBAC and membership rules
already enforced in the services.

**Decision.**
- ContextLedger issues and verifies short-lived ES256 JWTs. The algorithm is
  pinned, the key is selected by `kid` for rotation, and issuer, audience and
  all time claims are required.
- Agents are OAuth2 clients (client-credentials grant). Secrets are 256-bit,
  shown once and stored as salted scrypt hashes.
- Each agent acts through a dedicated service user that is a member with a
  non-ADMIN role. Permissions are the role's permissions intersected with the
  token scopes, enforced in the central `require_permission`. Privacy is the
  minimum of the role ceiling, the agent ceiling and the request.
- Every request re-checks revocation and membership in PostgreSQL. Revocation
  is therefore immediate, at the cost of one indexed lookup per request (Redis
  can cache it in Phase 14).
- A route-discovering sweep test calls every tenant route as three kinds of
  outsider and requires 403.
- Human SSO (OIDC federation) is out of scope. A dev-token endpoint and header
  mode exist for local use only, and both are refused outside local/test.

**Consequences.** JWTs are not revocable by themselves. Immediate revocation
comes from the per-request check, not from token lifetime. Short TTLs limit
exposure if a check is ever skipped.

---

## ADR-029: Redis for derived and short-lived state, with a failure policy per feature

**Status:** Accepted (Phase 14)

**Context.** Several API processes, workers and MCP servers need shared
short-lived state: cached search results, rate-limit counters, idempotency
records, OAuth `state` and MCP session state. PostgreSQL could hold all of it,
but that would put hot, disposable writes on the system of record.

**Decision.**
- Redis holds only derived, protective or short-lived state. Every key has a
  TTL and nothing in it is authoritative.
- One narrow store interface (`get`, `set` / `set NX`, `GETDEL`, `INCR` with a
  creation-time TTL) and one contract suite, run against both the Redis and the
  in-memory implementation.
- The failure policy is chosen per feature: the cache and rate limits fail
  open, idempotency keys and OAuth `state` fail closed.
- The retrieval cache is keyed by organization, generation and the caller's
  visible privacy scopes, and is consulted only after the PostgreSQL permission
  check. Writers bump the generation. The TTL bounds staleness for everything
  else. Decision snapshots bypass the cache.
- Idempotency is implemented as ASGI middleware scoped to the verified caller.
  It stores 2xx responses only and never records credential-issuing endpoints.
- Values are serialized as JSON / Pydantic, never pickle.

**Consequences.** Search results can be up to one TTL stale for writes that
bypass the invalidation paths, and for "now" queries. Fixed-window limits
allow bursts of up to twice the limit at window edges. Idempotency is
at-most-once per key while the reservation lock holds, not exactly-once.
Without `CONTEXTLEDGER_REDIS_URL` the in-memory store is correct only for a
single process, which is why staging and production require the URL.

---

## ADR-030: Domain events through a trigger-written outbox, keyed by tenant, consumed idempotently

**Status:** Accepted (Phase 15)

**Context.** Several reactions to changes (activity counters, cache
invalidation, later the embedding and graph pipelines) should run
asynchronously and independently of the request. Publishing to Kafka from the
request path can lose events, or publish events for rolled-back transactions.

**Decision.**
- PostgreSQL triggers write events into `event_outbox` in the same transaction
  as the change. A relay publishes them with `acks=all` from an idempotent
  producer, then deletes them.
- Three topics grouped by aggregate, keyed by organization id: per-tenant
  ordering and cross-tenant parallelism. Each has a dead-letter topic.
- A CloudEvents-style JSON envelope with Pydantic payload schemas, exported as
  JSON Schema and drift-tested. Payloads carry identifiers and metadata only,
  never content, because consumers do not enforce privacy scopes.
- Consumers are separate consumer groups. Dedup rows in `processed_events` are
  written in the same transaction as the effect, and offsets are committed
  after it. Transient errors get bounded exponential retries, then the DLQ.
  Permanent errors go straight to the DLQ.
- JSON rather than Avro/Protobuf with a schema registry: one producer and a few
  consumers in one repository, with schemas versioned in code. Revisit if
  independent teams consume the topics.

**Consequences.** Delivery is at-least-once, and the effect is applied once
only for PostgreSQL effects. Effects elsewhere must be idempotent. The activity
read model is eventually consistent. A hot tenant is bounded by one
partition. Several relays can interleave one tenant's events, so ordering
guarantees assume one relay.

## ADR-031: LLM features as an additive layer: provider interface, LangChain core only, code-verified grounding, a bounded in-house agent loop

**Status:** Accepted (additive AI capability, after Phase 15)

**Context.** Agents and people want answers and explanations in natural
language, but ContextLedger's value is that tenant, time, permission, version
and provenance are decided deterministically. An LLM must not become the place
where any of those is decided, and CI must not depend on a paid API.

**Decision.**
- One `GenerationProvider` interface (OpenAI Chat Completions with strict JSON
  schema output and tool calling; a scripted fake for tests). One deadline per
  call, bounded retries on 408/409/429/5xx, typed errors. Disabled by default.
- Retrieval runs first, through the existing services; the model receives only
  authorized facts, labelled F1..Fn. Citations are checked by code against the
  supplied labels (answers) or the ids tools returned (agent). Any invented
  citation withholds the answer (`ungrounded`); no facts means no model call.
- Prompts are immutable, versioned and fingerprinted; the version is recorded
  with every answer, evaluation result and agent trace.
- LangChain is used through `langchain-core` only: a `BaseChatModel` adapter
  over the provider, prompt templates, chains and `StructuredTool`s. No
  `langchain-openai`, so every call keeps the provider's controls. No LangGraph:
  the agent is a small loop in our code with step and tool-call limits.
- Agent tools are built per request with a server-fixed `TenantContext`, strict
  argument schemas without tenant fields, and uniform "not found or not
  accessible" errors. Tool calls go through services that re-check permissions
  and privacy.
- Evaluation separates deterministic pipeline metrics (free, run anytime) from
  live answer metrics (opt-in, paid, recorded with model, prompt, prices and
  commit). The dataset is synthetic and labelled as such.

**Consequences.** Answer quality depends on the model and is only measured by
live runs; none has been recorded yet. An in-house loop means no built-in
checkpointing or parallel tool execution; runs are short and bounded, so this
is acceptable now. The agent's trace stores tool arguments and outcomes but not
tool outputs, which may contain values readers of the trace are not allowed to
see; the answer text is stored and readable only by the requester.

## ADR-032: Contradictions are detected by a deterministic rule on write, never resolved automatically

**Status:** Accepted (Phase 16)

**Context.** Sources disagree (the CRM says ACTIVE, billing says SUSPENDED).
Each fact has one version chain, so a disagreeing source simply supersedes the
previous version; without a record of the conflict, the disagreement is lost
in an ordinary-looking update. Most updates, though, are real changes, and
flagging all of them would be noise.

**Decision.**
- One rule, `observed-value-conflict-v1`: a new version from a different source
  with a different value, valid from an instant at or before the time the
  previous source *observed* its value. It runs in `record_version`, in the
  same transaction as the write, so a committed conflicting version always has
  its contradiction row (and, through the outbox trigger, its
  `contradiction.detected` event).
- Nothing is discarded or changed: both versions stay in the history. The row
  records the preferred version (authority, then confidence, then later
  observation), but only a person closes it (resolved or dismissed), and that
  is recorded with who and why.
- A contradiction's privacy scope is the more sensitive of its two versions,
  and it is shown only to readers who may see both.
- LLM help is optional and limited to what rules cannot see: conflicts between
  different properties of one entity. The model gets only the facts the caller
  may see; each finding must name two supplied facts, and is stored as an open
  `semantic` suggestion with the prompt version as its detector.

**Consequences.** The rule depends on sources reporting `observed_at`; a
source that omits it (observed = valid_from) never triggers it, so a late,
conflicting report without observation times is treated as an update.
Numeric tolerance and cross-entity conflicts are not covered yet. The LLM
review's precision is unmeasured.

## ADR-033: Revocation is a separate append-only record that is bitemporal on read

**Status:** Accepted (Phase 17)

**Context.** Sometimes a recorded value was simply wrong. Superseding it with
a new version would say "it changed", which is false, and deleting it would
destroy the evidence of what decisions were based on.

**Decision.**
- A revocation is its own append-only row (`fact_revocations`: version, reason,
  who, when), at most one per version. Fact versions stay untouched.
- Reads treat revocation like any other transaction-time fact: a version
  revoked at R is excluded from "valid at T as known at K" when K >= R (and from
  "latest"), and still returned when K < R. The condition lives in
  `valid_at_condition`, so temporal queries, hybrid retrieval, grounded answers
  and new decision contexts all follow it.
- Decision receipts are not changed (their hash still verifies); each fact
  carries `revoked_at` when it was later revoked.
- Impact is computed in PostgreSQL from `context_snapshot_facts` and
  `decision_facts` in the revocation's transaction and stored in append-only
  `revocation_impacts` (which also emit `decision.impacted`). Reading an impact
  report recomputes it, flagging decisions recorded after the revocation from
  an older context.

**Consequences.** After revoking the latest version, a fact has no current
value until a new version is recorded, which must start later than the revoked
one. Impact is one hop (version → decisions); transitive effects are not
modelled. The Neo4j projection does not know about revocations yet.

## ADR-034: Prometheus metrics without tenant labels; OpenTelemetry traces opt-in

**Status:** Accepted (Phase 20)

**Context.** Operators need request rates, latencies, error ratios, retrieval
behaviour (vector fallback, cache) and model usage (calls, failures, tokens).
ContextLedger is multi-tenant and handles sensitive facts, so telemetry must
not become a side channel.

**Decision.**
- `prometheus-client` metrics on `GET /metrics` of the API. Labels are
  bounded: route templates (with the router prefix restored), status codes,
  vector/cache status, model, outcome class. No organization, user, query,
  value or prompt labels.
- `/metrics` needs a bearer token in staging and production (settings refuse
  to start otherwise, unless metrics are disabled).
- OpenTelemetry SDK with OTLP/HTTP export, only when an endpoint is
  configured; otherwise the API is a no-op. Standard instrumentation for
  FastAPI, SQLAlchemy and httpx, plus explicit spans for retrieval, answers,
  model calls and agent runs, with identifiers and counts only.
- Logs carry `trace_id` and `span_id` next to the existing `correlation_id`.
- Prometheus, Grafana (provisioned datasources and dashboard) and Jaeger run
  locally under a Compose profile, so the default stack is unchanged.

**Consequences.** Per-tenant usage is not visible in metrics (use the activity
read model or traces for that). Worker metrics and alert rules are follow-ups.
Tracing every SQL statement adds overhead; the sample ratio is configurable.

## ADR-035: Hash-locked dependencies, hardened images, scan in CI, attest releases

**Status:** Accepted (Phase 21)

**Context.** `pip install .` resolved whatever versions were newest at build
time, so two builds of one commit could differ, and a compromised or replaced
package file would be installed silently. Images were built and smoke-tested
but not scanned, and there was no release process.

**Decision.**
- Universal lock files with hashes, generated by `uv pip compile` from
  `pyproject.toml` and installed with `pip --require-hashes --no-deps`. CI
  regenerates them and fails on drift. pip stays the installer, so no new tool
  is needed at runtime.
- Dependencies get their own Docker layer. The API image runs as UID 10001,
  with a read-only root filesystem, no capabilities and `no-new-privileges`
  (proved by the CI smoke test). The web image uses Next.js standalone output.
- CI audits dependencies (pip-audit, npm audit), lints Dockerfiles (hadolint)
  and scans the API image (Trivy). Only fixable critical findings fail the
  build; high findings are reported, to avoid blocking on issues nobody can fix
  yet.
- Version tags publish multi-arch images to GHCR with SBOM, provenance and an
  attestation; Dependabot proposes updates weekly.

**Consequences.** Adding a dependency needs `make lock` (uv). The Trivy gate
can fail when a new critical CVE with a fix lands in the base image; the fix is
a rebuild or a Dependabot base-image bump. Tags, not digests, pin base images
and actions for now.

## ADR-036: Terraform for AWS foundations, built to be destroyed and recreated

**Status:** Accepted (Phase 22)

**Context.** The project must be deployable to AWS for demos without paying
for idle infrastructure, and without long-lived credentials or secrets in code.

**Decision.**
- Two Terraform stacks: `bootstrap` (state bucket, kept) and `core`
  (everything else, destroyed after demos). S3-native state locking instead of
  a DynamoDB table.
- No NAT gateway by default; an S3 gateway endpoint covers private S3 access.
  RDS is single-AZ, smallest Graviton class, private, encrypted, TLS-only.
- RDS generates and stores the master password in Secrets Manager; app secrets
  are created empty and filled with the CLI, so no secret enters Terraform
  state or git.
- GitHub Actions reaches AWS through OIDC with a role limited to pushing two
  ECR repositories from `main` and version tags; the runtime role is limited to
  its secrets, evidence objects and log groups.
- Defaults favour teardown (no deletion protection, forced deletion of buckets
  and repositories, zero secret recovery window) and keep a final database
  snapshot.
- Budget, tags and log retention are part of the stack, not a manual step.

**Consequences.** Single-AZ RDS and no NAT are not production-grade
availability; production would enable Multi-AZ, deletion protection and a
recovery window. Nothing inside the VPC can reach the internet until a NAT
gateway (or interface endpoints) is enabled in a later phase.

## ADR-037: EKS as an optional, destroyable deployment with Kustomize

**Status:** Accepted (Phase 23)

**Context.** The project needs a credible Kubernetes deployment path, but a running
cluster costs money every hour and this is a portfolio project without real traffic.

**Decision.** A separate Terraform stack (`eks`) reads the core stack's remote state and
adds only the cluster, a small Spot ARM node group, add-ons, Pod Identity and a database
ingress rule, so it can be destroyed without touching RDS, S3 or ECR. Manifests use plain
Kustomize (base + overlay + a separate migration Job) rather than Helm: there is one
deployment target, and plain YAML is easier to review. Redis and Neo4j run in-cluster
as ephemeral pods because their state is rebuildable; Kafka is not deployed and events
remain in the transactional outbox. There is no public load balancer; access is via
port-forward.

**Consequences.** Cheap to create and destroy, reviewable, validated in CI with
kubeconform without AWS credentials. Not highly available: single Redis/Neo4j pods,
fixed node count, no autoscaling, and client-side RDS certificate verification is still
a follow-up. None of it has been applied or measured under load yet.
