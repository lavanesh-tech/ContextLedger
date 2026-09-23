# REST API (v1)

Interactive docs: `http://127.0.0.1:8000/docs` (Swagger UI) and `/redoc` while
`make up` is running. The contract is committed as
[`api/openapi.json`](api/openapi.json), with a generated
[Postman collection](api/ContextLedger.postman_collection.json). A test fails
if either is stale (`make api-docs` regenerates both).

## Resources

All tenant data lives under `/api/v1/organizations/{organization_id}/…`.

| Area | Endpoints |
|---|---|
| Users | `POST /users`, `GET /users/me` |
| Organizations | `POST /organizations`; `GET/POST /organizations/{id}/members`; `PATCH/DELETE …/members/{user_id}` |
| Sources | `POST/GET …/sources`, `GET …/sources/{source_id}` |
| Facts | `POST …/facts`; `GET …/entities/{type}/{external_id}/facts?valid_at&known_at`; `…/timeline?known_at`; `…/changes?start&end&known_at`; `GET …/facts/{fact_id}/history`; `GET …/fact-versions/{id}/lineage` |
| Evidence | `POST …/evidence` (201 new, 200 same content already captured); `POST …/fact-versions/{id}/evidence`; `GET …/fact-versions/{id}/provenance` |
| Search | `POST …/search` |
| Decisions | `POST …/context-snapshots`; `POST …/decisions`; `GET …/decisions/{id}/receipt`; `GET …/fact-versions/{id}/decisions` |
| Provenance | `GET …/impact/fact-versions/{id}`, `…/impact/sources/{id}`, `…/impact/evidence/{id}`, `GET …/decisions/{id}/lineage` (503 if Neo4j is not configured) |

Timestamps must include a timezone. A naive `2026-01-15T09:00:00` is a 422,
because "valid at T" is ambiguous without one.

## Errors (RFC 9457)

Every error is `application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Forbidden",
  "status": 403,
  "code": "permission_denied",
  "detail": "role VIEWER lacks permission facts:write",
  "instance": "/api/v1/organizations/…/facts",
  "correlation_id": "0f0c…"
}
```

| Status | `code` | When |
|---|---|---|
| 401 | `authentication_required` | no or malformed identity |
| 403 | `permission_denied` | not a member (same answer for unknown organizations), or role lacks permission |
| 404 | `not_found` | resource does not exist *in this organization* |
| 409 | `conflict`, `invariant_violation` | duplicates; would leave the organization without an ADMIN |
| 422 | `request_invalid` (with `errors[]`), `validation_failed` | malformed request; domain validation |
| 500 | `internal_error` | unexpected; details only in server logs, find them with `correlation_id` |
| 503 | `service_unavailable` | optional dependency (Neo4j) not configured |

## Authentication (until Phase 13)

`auth_mode=development-headers` reads the caller's user id from
`X-ContextLedger-User-Id`. Settings validation refuses this mode in staging and
production. Phase 13 replaces it with JWT/OAuth. Every endpoint depends on one
function (`get_principal`), so only that function changes.

```bash
USER=$(curl -s -X POST localhost:8000/api/v1/users -H 'content-type: application/json' \
  -d '{"email":"ada@example.com","display_name":"Ada"}' | jq -r .id)
curl -s -X POST localhost:8000/api/v1/organizations -H "X-ContextLedger-User-Id: $USER" \
  -H 'content-type: application/json' -d '{"name":"Acme","slug":"acme"}'
```

## Privacy on read

Read endpoints apply the caller's role ceiling (VIEWER: PUBLIC and INTERNAL;
ENGINEER: + CONFIDENTIAL; ADMIN: all). Entity reads return
`withheld_by_privacy_scope` and provenance returns `withheld_evidence`, so a
partial answer is never mistaken for a complete one. Lineage of a version above
the ceiling is a 404, which neither confirms nor denies that it exists.
