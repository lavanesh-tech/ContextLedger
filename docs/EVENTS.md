# Domain events (Kafka)

PostgreSQL is the system of record. Kafka carries **notifications that something
changed**, so other parts of the system can react asynchronously without
coupling to the request path.

```
 request tx ──► fact_versions / evidence / decisions / ... (INSERT)
                 └─ trigger ──► event_outbox (same transaction)
 event-relay ──► claim outbox rows ─► Kafka (acks=all) ─► delete rows ─► commit
 event-consumers (one consumer group each)
   ├─ activity-projector ──────────► organization_activity_daily (PostgreSQL)
   └─ retrieval-cache-invalidator ─► retrieval-cache generation (Redis)
```

Code: `backend/app/events/` (schemas, messages, Kafka adapters, consumer
framework, handlers), `backend/app/workers/event_relay.py`,
`backend/app/workers/event_consumers.py`, migration `0010`.

## Why a transactional outbox

Writing to PostgreSQL and then publishing to Kafka from the request would fail
in two ways: a crash between the two loses the event, and a rolled-back
transaction may already have published one. Here, database triggers insert the
event into `event_outbox` **in the same transaction** as the change. A committed
change always has its event, and a rolled-back change never does. Because the
triggers live in the database, changes made outside the API (scripts,
backfills) emit events too.

## Topics and partitioning

| Topic | Events | Partitions (local) |
|---|---|---|
| `contextledger.facts.v1` | `fact.version_recorded`, `fact.embedding_stored`, `contradiction.detected` | 6 |
| `contextledger.evidence.v1` | `evidence.captured`, `evidence.linked` | 6 |
| `contextledger.decisions.v1` | `context.captured`, `decision.recorded` | 6 |
| `<topic>.dlq` | messages a consumer gave up on | 1 |

- **The message key is the organization id.** Every event of one tenant lands in
  one partition and is consumed in order. Different tenants spread across
  partitions, so consumers scale out by partition.
- Trade-off: one very busy tenant is limited to one partition's throughput
  (a "hot key"). A finer key (per entity) would scale further but give up
  per-tenant ordering, which the activity counters do not need but future
  consumers might. It can be revisited with load-test data (Phase 27).
- Topics are created explicitly (`make kafka-topics`, or automatically when the
  relay and consumers start). Broker auto-creation is disabled. The local broker
  has replication factor 1; a real cluster would use 3
  (`CONTEXTLEDGER_KAFKA_REPLICATION_FACTOR`).

## Event schema

Every message is a JSON envelope. The JSON Schemas of the envelope and of every
payload are in [`docs/events/schemas.json`](events/schemas.json), generated from
the Pydantic models by `make api-docs`, and a test fails if the committed file
is stale.

```json
{
  "specversion": "1.0",
  "id": "5b0f...",
  "type": "fact.version_recorded",
  "source": "contextledger",
  "schema_version": 1,
  "organization_id": "9c1e...",
  "subject": "the fact version id",
  "time": "2026-01-15T09:00:00.123456Z",
  "data": {
    "id": "...", "fact_id": "...", "version": 2, "source_id": "...",
    "supersedes_id": "...", "valid_from": "...", "valid_until": null,
    "authority": 90, "privacy_scope": "INTERNAL", "recorded_at": "..."
  }
}
```

Headers: `event_type`, `event_id`.

**Events carry identifiers and metadata, never content.** Fact values, evidence
excerpts, snapshot queries and decision outcomes and rationales stay in
PostgreSQL. Kafka consumers do not enforce privacy scopes, so a consumer that
needs content must read it through the services, which authorize the read. A
unit test fails if a payload model gains a content field.

**Evolution.** Payload models ignore unknown fields, so adding a field is
backward compatible. Removing or retyping a field bumps `schema_version`, and an
incompatible change gets a new `.v2` topic.

## Delivery guarantees (stated precisely)

- **Relay: at-least-once.** A batch is claimed with `FOR UPDATE SKIP LOCKED`,
  published with `acks=all` from an idempotent producer, and deleted in the
  same transaction. If Kafka fails, nothing is deleted. If the commit fails
  after Kafka acknowledged, the batch is published again.
- **Ordering.** With one relay, each tenant's events reach their partition in
  commit order. Several relays still deliver everything but may interleave one
  tenant's events.
- **Consumers: at-least-once delivery, effect applied once in PostgreSQL.**
  Each consumer inserts `(consumer, event_id)` into `processed_events` in the
  same transaction as its effect. A redelivered event finds the row and is
  skipped. Kafka offsets are committed only after that transaction commits (or
  the message is dead-lettered).
- **This is not exactly-once end to end.** Effects outside PostgreSQL (the Redis
  invalidation) can run more than once and are designed to be idempotent.
  Kafka transactions are not used, because the effects live in PostgreSQL and
  Redis, not in Kafka.

Tests show each of these guarantees: redelivered and concurrent duplicate
events are counted once, a rolled-back change emits nothing, and a failing
broker leaves events queued (`tests/integration/test_events.py`). A real-broker
round trip runs in `test_kafka_roundtrip.py`.

## Retries and the dead-letter topic

| Failure | Handling |
|---|---|
| Undecodable message, unknown event type, invalid payload | straight to `<topic>.dlq`: retrying cannot fix it |
| `PermanentEventError` raised by a handler | straight to the DLQ |
| Any other exception (database hiccup, ...) | retried with exponential backoff (`event_consumer_max_attempts` = 3, base 0.5 s), then DLQ |
| DLQ publish fails | the consumer restarts without committing the offset, and the message is redelivered |

A failed attempt rolls back its transaction, including the dedup row, so
nothing is half-applied. Dead-lettered messages keep their key, value and
headers, plus `dlq_consumer`, `dlq_error`, `dlq_attempts` and `dlq_source`
(`topic/partition/offset`), so they can be inspected and replayed once the
cause is fixed. Retries block their partition (ordering is preserved), which
is why they are few and short.

## Consumers

| Consumer (= group id) | Subscribes to | Effect |
|---|---|---|
| `activity-projector` | fact.version_recorded, evidence.captured, decision.recorded | `+1` on `organization_activity_daily` for the event's UTC day. The counters are not idempotent, which is exactly what `processed_events` protects |
| `retrieval-cache-invalidator` | fact.version_recorded, fact.embedding_stored | bumps the tenant's retrieval-cache generation, which also catches writes that bypass the API |

`GET /api/v1/organizations/{id}/activity?days=30` returns the counters plus
`pending_events` (this tenant's events still in the outbox), so a caller can
tell "zero" from "not counted yet". The read model is **eventually
consistent**, and it is tenant-scoped and authorized like every other endpoint.

## Running it

```bash
make up              # starts kafka, event-relay and event-consumers with the stack
make kafka-topics    # (re)create topics; idempotent
make event-relay     # or run on your Mac against the Compose broker
make event-consumers
```

Not yet: moving the embedding worker and the graph projector from polling
their own queues to consuming these events, and outbox-lag and consumer-lag
metrics (Phase 20).
