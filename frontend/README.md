# ContextLedger web UI (Phase 19)

Next.js 15 (App Router) + React 19 + TypeScript (strict). A thin client of the
FastAPI REST API: every rule (tenant, role, scopes, privacy, time) is enforced
by the API, never by the UI.

```bash
cd frontend
npm ci
npm run dev          # http://localhost:3000, forwards /api/v1/* to the API
npm run check        # tsc --noEmit + vitest + next build
```

The API address is `CONTEXTLEDGER_API_URL` (default `http://127.0.0.1:8000`,
i.e. `make run`). Requests are forwarded by Next.js rewrites, so the API needs
no CORS configuration and its address is not in the browser bundle.

Pages: connection settings, hybrid temporal search (valid at / as known at,
score breakdown), entity timeline (valid time and transaction time), decision
receipts (hash verification, relied-on and revoked facts), contradictions
(resolve or dismiss), revocations (revoke, impact on decisions) and grounded
answers.

Credentials are the local-development user-id header or a bearer token, kept
in this browser's localStorage only. Use bearer tokens anywhere but a local
machine; the header is refused by the API outside local development.
