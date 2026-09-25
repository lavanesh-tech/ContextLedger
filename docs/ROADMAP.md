# Roadmap

Built strictly phase by phase. A phase is **Done** only after its verification
(lint, types, tests, Docker where relevant) has actually been run.

| # | Phase | Status |
|---|-------|--------|
| 1 | Repository foundation: FastAPI, settings, JSON logging, correlation IDs, health, Docker Compose (Postgres+pgvector, Redis, Neo4j, Kafka), Ruff, mypy, pytest, CI stub | Done |
| 2 | Async SQLAlchemy, session lifecycle, readiness checks, Alembic, DB tests | Done |
| 3 | Organizations, users, memberships, roles, tenant ownership | Done |
| 4 | Temporal fact domain: Entity, Fact, FactVersion, FactSource, constraints, supersession | Done |
| 5 | Temporal resolution engine: current, valid-at-T, as-known-at-K, history, diff T1..T2, lineage | Done |
| 6 | Sources and evidence: immutable, content-addressed evidence linked to fact versions | Done |
| 7 | pgvector, embeddings, OpenAI provider, embedding jobs, HNSW vs IVFFlat | Done |
| 8 | Hybrid temporal RAG: vector + full-text + metadata + temporal + tenant filters | Done |
| 9 | Decision receipts: ContextSnapshot, Decision, DecisionFact | Done |
| 10 | Neo4j provenance graph and impact traversal | Done |
| 11 | MCP server and core tools | Done |
| 12 | Complete REST API, standardized errors, Swagger, Postman | Done |
| 13 | JWT, OAuth2, RBAC, tenant/agent/retrieval authorization, cross-tenant tests | Done |
| 14 | Redis: retrieval cache, rate limiting, OAuth state, idempotency, MCP state | Done |
| 15 | Kafka events and idempotent consumers | Done |
| 16 | Contradiction detection | Done |
| 17 | Revocation impact | Planned |
| 18 | RAG evaluation: Recall@K, Precision@K, MRR, temporal/authorization correctness | Planned |
| 19 | Frontend: Next.js + React + TypeScript in `frontend/`, consuming the FastAPI `/api/v1` REST API (ADR-008) | Planned |
| 20 | Observability: OpenTelemetry, Prometheus, Grafana | Planned |
| 21 | Production Docker hardening + full CI/CD | Planned |
| 22 | Terraform + core AWS (VPC, RDS, S3, ECR, Secrets Manager, IAM, CloudWatch) | Planned |
| 23 | EKS deployment + teardown docs | Planned |
| 24 | ECS/Fargate batch workload | Planned |
| 25 | DynamoDB (only if justified) | Planned |
| 26 | Athena (only if justified) | Planned |
| 27 | Performance / load testing | Planned |
| 28 | Security review | Planned |
| 29 | AWS recruiter demo lifecycle: deploy → demo → destroy → verify → recreate | Planned |
| 30 | Portfolio polish | Planned |
| 31 | Full project teaching | Planned |

## Additive AI capability (outside the numbered phases)

Built after Phase 15 without renumbering or replacing any phase. Phase 18 (RAG evaluation of retrieval: Recall@K,
Precision@K, MRR) are still planned as defined above. Details: [AI.md](AI.md),
ADR-031.

| Part | Status |
|---|---|
| Generation provider: OpenAI Chat Completions with strict JSON schema, retries, typed errors, fake for tests | Done |
| Versioned prompts and grounded answers with deterministic citation checks (`POST …/answers`) | Done |
| LangChain orchestration (`langchain-core` adapter and chains; no LangGraph) | Done |
| Grounded-answer evaluation: deterministic pipeline metrics (recorded) and live answer metrics (opt-in, paid) | Done (live run not yet recorded) |
| Historical Decision Investigator agent with read-only tools, limits and traces (`POST …/investigations`) | Done |
| MCP tools `answer_question` and `investigate_decision` | Done |

## Known follow-ups carried forward

- Add a dependency lock file (e.g. `uv lock`) for reproducible installs (by Phase 21).
- Split dependency installation into its own Docker layer for faster rebuilds (Phase 21).
- Pin container images by digest (Phase 21).
- Tune DB pool size and statement timeout with load-test measurements (Phase 27).
- Restrict who can call the readiness endpoint, or trim its detail, in production (Phase 28).
- PostgreSQL Row-Level Security as a second tenant-isolation layer (evaluate in Phase 13 / 28).
- Time-ordered UUIDv7 primary keys once the runtime supports them natively (index locality).
- Audited redaction process for evidence that must be removed (legal takedown) (Phase 28).
- Trigger the embedding worker from `fact.version_recorded` events instead of polling only.
- Tune `hnsw.ef_search` from retrieval-quality and load-test measurements (Phases 18, 27).
- Benchmark IVFFlat vs HNSW on a growing table (index built on a small prefix, then inserts) to confirm or revisit ADR-020.
- Evaluate a reranker (cross-encoder or LLM) against RRF-only retrieval with Phase 18 metrics.
- Expose retrieval over REST (Phase 12) and MCP (Phase 11); agent-specific privacy ceilings (Phase 13).
- Sign decision receipts with a key held outside the database (e.g. AWS KMS), optionally chain receipt hashes (Phase 28).
- Feed the graph projector from Kafka events instead of its own outbox; alert on outbox size, projection lag and consumer lag (Phase 20).
- A DLQ replay command, and a retention job for `processed_events` (older than Kafka retention).
- MCP over streamable HTTP authenticated with agent access tokens (MCP authorization spec) (Phase 21).
- Human SSO through an external OIDC provider (JWKS verification) instead of dev tokens.
- Record a live grounded-answer evaluation (`make eval-live`) and compare prompt versions on answer metrics before changing the default prompt.
- An investigator evaluation dataset (tool-use correctness, grounded rate) alongside the answer evaluation.
- Per-tenant token budgets and rate limits for the LLM endpoints (Phase 27/28).
- More contradiction rules (numeric tolerance, cross-entity), and measuring the LLM review's precision on a labelled set before relying on it.
- Tune rate limits and the retrieval-cache TTL from load tests; consider a sliding-window limiter (Phase 27).
- Measure the retrieval-cache hit rate and latency with a real workload (Phases 20, 27).
- Cache agent-revocation and membership checks in Redis only if load tests show they matter (Phase 27).
- Authorization-code + PKCE flow for remote MCP clients using the OAuth state store (with the HTTP MCP transport).
