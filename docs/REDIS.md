# Redis: shared, short-lived state

PostgreSQL is the system of record. Redis holds only state that is **derived**
(cached search results), **protective** (rate-limit counters, idempotency
records) or **short-lived by design** (OAuth `state`, MCP session state).
Every key has a TTL, persistence is off in the local stack, and losing all of
Redis loses no business data.

Code: `backend/app/cache/` (store, retrieval cache, rate limiter, idempotency
middleware, OAuth state) and `backend/app/mcp/state.py`.

## One small store interface

`KeyValueStore` offers `get`, `set` (optionally only-if-absent), atomic
`get_and_delete`, `delete` and atomic `increment` with a TTL fixed at creation.
Each feature is built from these single atomic operations, so no feature needs
a lock or a multi-step transaction.

| Implementation | Used when | Scope |
|---|---|---|
| `RedisStore` (redis-py asyncio) | `CONTEXTLEDGER_REDIS_URL` is set (required in staging and production) | shared by every API process, worker and MCP server |
| `MemoryStore` | no URL (a single local process, unit tests) | one process only |

The same contract tests (`tests/store_contract.py`) run against both: against
`MemoryStore` in unit tests, and against a real Redis in `test_redis_store.py`
(CI has a Redis service). Redis errors surface as `StoreUnavailableError`, so
each feature states its own failure policy explicitly.

## Features and failure policies

| Feature | Keys | TTL | If Redis is down |
|---|---|---|---|
| Retrieval cache | `cl:retrieval:v1:{org}:{generation}:{sha256}` and `cl:retrieval:gen:{org}` | `retrieval_cache_ttl_seconds` (60) | **fail open**: every lookup is a miss and the search runs in PostgreSQL |
| Rate limiting | `cl:ratelimit:{user\|agent\|oauth-client}:{id}` | 60 s window | **fail open**: requests proceed and a warning is logged |
| Idempotency keys | `cl:idempotency:{sha256(caller, key)}` | lock `idempotency_lock_seconds` (60), record `idempotency_ttl_seconds` (24 h) | **fail closed**: a request that carries a key gets 503 |
| OAuth `state` | `cl:oauth:state:{sha256(state)}` | `oauth_state_ttl_seconds` (600) | **fail closed**: the flow is refused |
| MCP session state | `cl:mcp:session:{org}:{user}:{agent}:{session}` | `mcp_session_ttl_seconds` (1 h, sliding) | writes are skipped; `record_decision` needs an explicit `snapshot_id` |

Why these choices. A cache or limiter outage should not become an API outage,
so both fail open. An idempotency key or an OAuth `state` is a promise to the
client, and it can only be kept by refusing, so both fail closed.

## Retrieval cache

A search result is cached under a key built from everything that can change
the answer: organization, the organization's **generation** number, the privacy
scopes the caller may see, embedding model, normalized query, limit, filters,
trust weight, and `valid_at` / `known_at` when pinned.

- **Authorization before the cache.** The permission check (membership, role,
  token scopes, agent revocation) runs in PostgreSQL before the lookup. The key
  includes the *visible privacy scopes*, so a VIEWER and an ADMIN never share an
  entry. Callers with the same scopes do share entries, which is safe because
  the result depends only on the organization and those scopes.
- **Invalidation by generation.** Recording a fact version (REST) and storing
  new embeddings (worker) increment `cl:retrieval:gen:{org}`. Every older entry
  becomes unreachable at once, with no key scan. The generation is read
  *before* PostgreSQL is queried, so a result computed while a write commits is
  stored under the old generation and never served as current.
- **Bounded staleness.** Writes that bypass these paths (a script inserting
  rows directly) are covered only by the TTL. So are "now" queries: a version
  whose `valid_until` passes stays cached for up to the TTL (60 s by default).
- **What is not cached.** Decision snapshots always read PostgreSQL, because a
  receipt must show what was actually retrieved at decision time. Degraded
  (full-text only) answers are not cached either.
- Responses report `"cache": "hit" | "miss" | "off"`.

Results are serialized with a Pydantic `TypeAdapter`, never with pickle, so a
compromised cache cannot execute code in the API. An entry that no longer
decodes is treated as a miss.

## Rate limiting

A fixed window of one minute, applied per authenticated principal: each agent
client and each user has its own budget (`rate_limit_requests_per_minute`,
default 600). It is enforced right after authentication, and every process
increments the same counter. `/oauth/token` is also limited per `client_id`
(`rate_limit_token_requests_per_minute`, default 30), whether or not the client
exists, which caps client-secret guessing.

Exceeding a limit returns `429` problem details with `Retry-After`,
`RateLimit-Limit` and `RateLimit-Remaining: 0`. The token endpoint answers in
the OAuth error format (`{"error": "too_many_requests"}`).

Trade-off: a fixed window lets a client send up to twice the limit across a
window boundary. It costs one atomic round trip (`INCR`, `PEXPIRE NX`, `PTTL`
in one `MULTI`). A sliding-window or token-bucket Lua script would be smoother
at the cost of more complexity. The defaults are starting points, not tuned
numbers. Load tests in Phase 27 should set them.

## Idempotency keys

`POST` requests may send `Idempotency-Key: <1-255 printable ASCII>`:

1. The first request reserves the key with `SET NX` (a short lock) and a
   SHA-256 fingerprint of method, path, query and body.
2. On a 2xx, the status, `Content-Type` / `Location` / `ETag` and body are
   stored for 24 h. Any other outcome releases the key, so the client can fix
   the request and retry with the same key.
3. A retry with the same key and the same request gets the stored response and
   `Idempotent-Replayed: true`. The endpoint does not run again.
4. The same key while the first request runs: `409 idempotency_key_in_use`
   with `Retry-After: 1`. The same key with a different request:
   `422 idempotency_key_reused`. A malformed key: `400 idempotency_key_invalid`.

Keys are scoped to the **verified** caller: the user id, or the agent client id
from a validated token. A refreshed token keeps the same scope, and one caller
cannot replay or block another caller's keys. Requests with invalid
credentials pass through untouched, and the endpoint answers 401.
`/oauth/token` and `/auth/dev-token` are never recorded, because their
responses contain credentials.

Limits, stated plainly: this is at-most-once execution **per key while the
reservation holds**, not exactly-once delivery. If a request runs longer than
`idempotency_lock_seconds`, a retry can execute it a second time. Responses
over 1 MiB are not stored.

## OAuth `state`

`OAuthStateStore.issue(payload)` returns a 256-bit random `state` bound to a
payload (client, redirect URI, PKCE challenge). `consume(state)` returns the
payload exactly once through one atomic `GETDEL`, so two concurrent callbacks
cannot both succeed. Only a SHA-256 of the state is stored. The client
credentials grant needs no `state`. This store is for redirect-based flows
(the authorization-code flow for remote MCP clients, planned with the HTTP MCP
transport).

## MCP session state

One stdio MCP server process serves one client session and gets a random
session id. Across tool calls it remembers the last captured snapshot (and its
fact_version_ids) and the recent search queries, with a sliding TTL.

- `record_decision` may omit `snapshot_id`, and then uses the session's last
  snapshot. The id is still validated by the decision service exactly as if
  the agent had passed it (tenant, permission, citations inside the snapshot).
- `get_session_context` shows what the session remembers.

Losing this state costs convenience only: the agent passes `snapshot_id` itself.

## Operations

- Local: `make up` starts Redis with a password and no persistence. The API
  container gets `CONTEXTLEDGER_REDIS_URL=redis://:$REDIS_PASSWORD@redis:6379/0`.
  `make run` on the host uses the in-memory store unless you set
  `CONTEXTLEDGER_REDIS_URL` (e.g. `redis://:<password>@127.0.0.1:6379/0`).
- Tests use database 15 (`CONTEXTLEDGER_TEST_REDIS_URL`), never the stack's 0.
- The URL is a `SecretStr` (it can contain a password). Use `rediss://` for TLS
  in AWS (ElastiCache, Phase 22).
- Readiness does not depend on Redis. The cache and limiter fail open, and
  idempotent requests report their own 503.
