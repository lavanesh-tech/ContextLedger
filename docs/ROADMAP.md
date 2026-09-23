# Roadmap

Built strictly phase by phase. A phase is **Done** only after its verification
(lint, types, tests, Docker where relevant) has actually been run.

| # | Phase | Status |
|---|-------|--------|
| 1 | Repository foundation: FastAPI, settings, JSON logging, correlation IDs, health, Docker Compose (Postgres+pgvector, Redis, Neo4j, Kafka), Ruff, mypy, pytest, CI stub | In review |
| 2 | Async SQLAlchemy, session lifecycle, readiness checks, Alembic, DB tests | Planned |
| 3 | Organizations, users, memberships, roles, tenant ownership | Planned |
| 4 | Temporal fact domain: Entity, Fact, FactVersion, FactSource, constraints, supersession | Planned |
| 5 | Temporal resolution engine: current, valid-at-T, history, diff T1..T2, lineage | Planned |
| 6 | Sources and evidence | Planned |
| 7 | pgvector, embeddings, OpenAI provider, embedding jobs, HNSW vs IVFFlat | Planned |
| 8 | Hybrid temporal RAG: vector + full-text + metadata + temporal + tenant filters | Planned |
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
