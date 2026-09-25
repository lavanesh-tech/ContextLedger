# CI/CD and container images (Phase 21)

Design decision: ADR-035 in [DECISIONS.md](DECISIONS.md).

## Reproducible dependencies

- `backend/requirements.lock` (runtime) and `backend/requirements-dev.lock`
  (with dev tools) pin every package, transitive ones included, **with
  hashes**, resolved for all platforms (`uv pip compile --universal`).
- Installs use `pip install --require-hashes --no-deps -r <lock>`: a file that
  does not match its hash is refused, and nothing outside the lock is pulled in.
- `make lock` re-resolves both files after a change to `pyproject.toml` (needs
  `uv`). CI fails when a lock file no longer matches `pyproject.toml`.
- The web UI uses `package-lock.json` with `npm ci`.

## Images

| Image | Build | Runtime |
|---|---|---|
| `contextledger-api` (`backend/Dockerfile`) | Lock file installed in its own layer (rebuilt only when dependencies change), then the app | `python:3.12-slim-bookworm`, no build tools or tests, UID 10001, health check, migrations included for a one-off `alembic upgrade head`, runs with a read-only root filesystem |
| `contextledger-web` (`frontend/Dockerfile`) | `npm ci` from the lock file, `next build` with standalone output | Only the standalone server and static files, non-root `node` user |

`make docker-build` builds both. The web UI also runs in Compose:
`docker compose --profile web up -d web` (http://localhost:3000, read-only,
all capabilities dropped).

## CI (`.github/workflows/ci.yml`, every push and pull request)

| Job | Steps |
|---|---|
| backend | hash-checked install; lock files match `pyproject.toml`; pip-audit on the runtime lock; ruff; mypy --strict; Alembic upgrade, drift check, downgrade and upgrade; pytest with PostgreSQL + pgvector, Redis, Kafka and Neo4j services |
| frontend | `npm ci`; `npm audit --omit=dev --audit-level=high`; type-check; unit tests; production build |
| docker | Compose file validation; hadolint on both Dockerfiles; build both images; Trivy scan of the API image (fails on fixable CRITICAL findings, reports HIGH); smoke test of the API container run read-only with all capabilities dropped (liveness 200, readiness 503 without a database) |

## Releases (`.github/workflows/release.yml`)

Pushing a tag `vX.Y.Z` (or running the workflow by hand) builds both images
for `linux/amd64` and `linux/arm64` and pushes them to GitHub Container
Registry as `ghcr.io/<owner>/contextledger-api` and `…/contextledger-web`,
tagged `X.Y.Z`, `X.Y` and the short commit SHA, with an SBOM, build provenance
(`mode=max`) and a GitHub build-provenance attestation.

```bash
git tag v0.21.0
git push origin v0.21.0
```

Deployment to AWS comes in Phases 22–24.

## Dependency updates

`.github/dependabot.yml` opens weekly pull requests for Python, npm, Docker
base images and GitHub Actions. Each one runs the full CI above.

## Not done yet

- Base images and third-party actions are pinned by tag, not digest or commit SHA.
- Release images are attested but not signed with cosign.
