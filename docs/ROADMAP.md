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
| 8 | Hybrid temporal RAG: vector + full-text + metadata + temporal + tenant filters | In review |
| 9 | Decision receipts: ContextSnapshot, Decision, DecisionFact | Planned |
| 10 | Neo4j provenance graph and impact traversal | Planned |
| 11 | MCP server and core tools | Planned |
| 12 | Complete REST API, standardized errors, Swagger, Postman | Planned |
| 13 | JWT, OAuth2, RBAC, tenant/agent/retrieval authorization, cross-tenant tests | Planned |
| 14 | Redis: retrieval cache, rate limiting, OAuth state, idempotency, MCP state | Planned |
| 15 | Kafka events and idempotent consumers | Planned |
| 16 | Contradiction detection | Planned |
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

## Known follow-ups carried forward

- Add a dependency lock file (e.g. `uv lock`) for reproducible installs (by Phase 21).
- Split dependency installation into its own Docker layer for faster rebuilds (Phase 21).
- Pin container images by digest (Phase 21).
- Correlation ID header on Starlette-generated 500 responses (Phase 12).
- Tune DB pool size and statement timeout with load-test measurements (Phase 27).
- Restrict who can call the readiness endpoint, or trim its detail, in production (Phase 28).
- PostgreSQL Row-Level Security as a second tenant-isolation layer (evaluate in Phase 13 / 28).
- Time-ordered UUIDv7 primary keys once the runtime supports them natively (index locality).
- Audited redaction process for evidence that must be removed (legal takedown) (Phase 28).
- Trigger the embedding worker from Kafka fact events instead of polling only (Phase 15).
- Tune `hnsw.ef_search` from retrieval-quality and load-test measurements (Phases 18, 27).
- Benchmark IVFFlat vs HNSW on a growing table (index built on a small prefix, then inserts) to confirm or revisit ADR-020.
- Evaluate a reranker (cross-encoder or LLM) against RRF-only retrieval with Phase 18 metrics.
- Expose retrieval over REST (Phase 12) and MCP (Phase 11); agent-specific privacy ceilings (Phase 13).
