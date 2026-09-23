# Authentication and Authorization

## Who can call the API

| Caller | How it authenticates | Bound to |
|---|---|---|
| **Agent** | OAuth2 client credentials → short-lived JWT (`POST /api/v1/oauth/token`) | exactly one organization |
| **User (local/test)** | `POST /api/v1/auth/dev-token` → JWT, or the `X-ContextLedger-User-Id` header in `development-headers` mode | any organization they are a member of |
| **User (production)** | tokens from an external identity provider (OIDC); not implemented in this project, see the roadmap | same |

`development-headers` mode and the dev-token endpoint are refused in staging
and production, by settings validation and by returning 404.

## Access tokens

ES256-signed JWTs issued by ContextLedger (`app/auth/tokens.py`), 15 minutes by default.

| Claim | Meaning |
|---|---|
| `iss`, `aud` | fixed per deployment; always verified |
| `sub` | `user:<uuid>` or `agent:<agent client uuid>` |
| `uid` | the user the request acts as (for agents: their service user) |
| `org` | agents only: the one organization the token is valid for |
| `scope` | agents only: permissions granted (they narrow the role) |
| `privacy` | agents only: the most sensitive privacy scope readable |
| `iat`, `nbf`, `exp`, `jti` | required, with 30 s clock leeway |

Verification pins `ES256` (so `alg: none` and HMAC-with-public-key confusion
are rejected, both covered by tests), picks the key by `kid`, and requires every
time claim. **Rotation:** set a new `CONTEXTLEDGER_JWT_SIGNING_KEY`/`_KEY_ID` and
put the old public key in `CONTEXTLEDGER_JWT_PREVIOUS_PUBLIC_KEYS` until the
old tokens have expired. `make jwt-key` prints a new private key.

## Agents

An ADMIN registers an agent (`POST /organizations/{id}/agent-clients`) with:

* a **role** (ENGINEER or VIEWER, never ADMIN),
* **scopes**, which must be permissions the role has,
* a **privacy ceiling** (default INTERNAL).

The response shows `client_secret` **once**. Only a salted scrypt hash is
stored. The agent exchanges its credentials for a token, optionally asking for
fewer scopes (`scope=facts:read`).

Each agent acts through its own **service user**, which is a member of the
organization with the agent's role. Every existing check therefore applies to
agents unchanged: RBAC, the membership re-check on each request, and the
last-ADMIN invariant. On top of that:

```text
effective permissions = role permissions  ∩  token scopes
effective privacy     = min(role ceiling, agent ceiling, request max_privacy_scope)
```

On **every** request, a token is refused (403) when:

* it is used for an organization other than its `org`,
* the agent client has been revoked (even though the JWT has not expired),
* the service user is no longer a member, or has been made ADMIN.

## Tenant isolation, tested exhaustively

`tests/integration/test_cross_tenant_sweep.py` discovers every route under
`/organizations/{organization_id}` from the application itself and calls each
method as:

* a member of another organization,
* an agent of another organization,
* a user with no memberships.

Every call must return 403, so a new endpoint is covered as soon as it exists.
Unknown organizations get the same 403 as foreign ones, so ids cannot be probed.

## Error formats

API errors use RFC 9457 problem details. The token endpoint uses the OAuth2
format from RFC 6749 §5.2 (`{"error": "invalid_client", ...}`) with
`Cache-Control: no-store`, because OAuth client libraries expect that format.
