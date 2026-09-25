# Security review

Phase 28. A structured self-review of ContextLedger. It is **not** a third-party
audit or a penetration test. Every "fixed" item links to code and an automated check,
and everything that is still open is listed as open.

Review date: 2026-09-25. Scope: backend API, MCP server, AI features, workers,
frontend, container images, CI/CD, Terraform and Kubernetes manifests.

## Assets and trust boundaries

| Asset | Why it matters |
|---|---|
| Tenant facts, evidence, decisions, receipts | Business data; one tenant must never see another's |
| Decision receipts and fact history | Audit value depends on immutability |
| Signing key (ES256), agent client secrets, OpenAI key, DB credentials | Compromise means impersonation, spend or data access |
| Cloud account | Cost and blast radius |

Trust boundaries: browser → Next.js → API; MCP client (agent) → API; API → PostgreSQL,
Redis, Neo4j, Kafka; API → OpenAI (outbound, optional); CI → AWS (OIDC); operator → AWS
and Kubernetes APIs.

## Threats and controls (STRIDE)

| Threat | Control in place | Evidence |
|---|---|---|
| **Spoofing:** forged or stolen tokens | ES256 JWTs pinned to one algorithm; `iss`, `aud`, `exp`, `nbf`, `jti` required; key rotation by `kid`; short TTL (15 min). Agent secrets are 256-bit, stored as scrypt hashes and compared in constant time | `app/auth/tokens.py`, `app/auth/secrets.py`, `tests/unit/test_auth_tokens.py` |
| **Spoofing:** development header auth used in production | `development-headers` mode is refused at startup in staging/production | `app/core/config.py`, `tests/unit/test_config.py` |
| **Tampering:** rewriting fact history or receipts | Append-only tables enforced by database triggers; changes go through revocations; receipts are hash-sealed | migrations 0001–0013, docs/REVOCATION.md, docs/DECISION_RECEIPTS.md |
| **Tampering:** injection | ORM and bound parameters only; the LLM never receives SQL or database handles | repositories, docs/AI.md |
| **Repudiation** | Correlation IDs on every request, structured logs, `agent_runs` traces, outbox events | docs/OBSERVABILITY.md, docs/EVENTS.md |
| **Information disclosure:** cross-tenant access | Every query filters by tenant from the authenticated context; RBAC per organization; privacy scopes; a cross-tenant sweep test calls every tenant route as an outsider | `tests/integration/test_tenant_isolation.py`, `test_cross_tenant_sweep.py` |
| **Information disclosure:** prompt injection through stored facts | Tenant and user are fixed server-side (never from model output); agent tools are read-only and bounded; answers are cited and citations verified; the model gets no SQL | docs/AI.md, `tests/unit/test_investigator.py`, `test_grounding.py` |
| **Information disclosure:** secrets in logs, metrics or git | Secrets are `SecretStr` and never logged; no tenant IDs in metric labels; `.env` git-ignored; gitleaks on full history in CI | `tests/unit/test_config.py` (password never in repr or rendered URL), `tests/api/test_observability.py`, CI `secrets` job |
| **Denial of service** | Per-caller rate limits (Redis); request body limit (413); statement timeout; bounded agent steps and tool calls; retrieval limits | `app/cache/rate_limit.py`, `app/core/security.py` |
| **Elevation of privilege** | Roles checked in services; containers non-root, read-only root FS, all capabilities dropped; Pod Security `restricted`; least-privilege IAM; CI via OIDC restricted to main and tags | Dockerfiles, `infrastructure/`, docs/CI_CD.md |

## Findings

| ID | Finding | Severity (own rating) | Status |
|---|---|---|---|
| F-01 | API responses lacked standard security headers | Low | **Fixed**: `SecurityHeadersMiddleware` (nosniff, frame deny, no-referrer, deny-all CSP, `no-store`, HSTS outside local). Tests: `tests/api/test_security_hardening.py` |
| F-02 | No request body size limit: large JSON bodies were parsed in full | Medium | **Fixed**: `BodySizeLimitMiddleware` returns 413 above `CONTEXTLEDGER_MAX_REQUEST_BODY_BYTES` (1 MiB) by Content-Length or while streaming. Tests included |
| F-03 | EKS Kubernetes Secrets not envelope-encrypted with a customer-managed key | Medium | **Fixed**: KMS key with rotation, `encryption_config` on the cluster |
| F-04 | Database TLS was not certificate-verified (RDS forces TLS, the client didn't verify) | Medium | **Fixed for EKS**: `CONTEXTLEDGER_DB_SSL_MODE=verify-full` with the AWS RDS CA bundle (ConfigMap `rds-ca`); the API, workers and migrations all use it. Staging/production refuse `disable` |
| F-05 | ECS batch task uses `require` (encrypted, not verified) | Low | **Open**: needs the CA bundle in the image or an init container. Follow-up |
| F-06 | Frontend served without security headers | Low | **Fixed partly**: CSP and headers in `next.config.ts`. **Open**: `script-src 'unsafe-inline'` remains until a nonce-based CSP |
| F-07 | Possible secrets in git history | n/a | **Checked**: gitleaks over all 80 commits found no leaks (2026-09-25); now a CI gate |
| F-08 | IaC and Dockerfiles not scanned for misconfiguration | Low | **Fixed**: `trivy config` gate in CI (HIGH/CRITICAL); accepted items in `.trivyignore.yaml` with reasons |
| F-09 | Tenant isolation is enforced in the application only, not by PostgreSQL row-level security | Medium | **Open**: covered by isolation and sweep tests, but RLS would be defense in depth |
| F-10 | EKS API endpoint is public (CIDR-restricted) | Accepted | Demo without VPN or bastion; private endpoint also on |
| F-11 | HTTPS egress to 0.0.0.0/0 from app security groups | Accepted | Needed for ECR, Secrets Manager, CloudWatch, OpenAI without paid VPC endpoints; no ingress |
| F-12 | S3 uses SSE-S3, not a customer-managed KMS key | Accepted | No cross-account or key-audit requirement |
| F-13 | Alerts SNS topic not encrypted | Accepted | Alarm metadata only; encryption needs a CMK with service key policies |
| F-14 | Neo4j container root filesystem writable | Accepted | Demo-only, rebuildable projection; non-root, no capabilities |

## Automated checks (all in CI)

| Check | Tool | Gate |
|---|---|---|
| Python dependencies | pip-audit against the hash-locked lock file | any known vulnerability |
| npm dependencies | `npm audit --omit=dev` | high and above |
| Container images | Trivy image scan | HIGH/CRITICAL fixable |
| IaC and Dockerfiles | `trivy config` with `.trivyignore.yaml` | HIGH/CRITICAL not accepted |
| Secrets in history | gitleaks (full history) | any finding |
| Python code | Ruff `S` (bandit) rules | any finding |

Run them locally with `make security` (needs `brew install trivy gitleaks`).

## Not covered

No external penetration test, no fuzzing, no formal threat-model review by a second
person, and no runtime detection (GuardDuty, Falco). No AWS environment has been
applied yet, so cloud controls are validated statically only.
