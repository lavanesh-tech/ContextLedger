# Performance and load testing

Phase 27 adds an HTTP load test of the running API. It complements the in-process
benchmarks (`make bench-vector`, `make bench-retrieval`, docs/BENCHMARKS.md), which
time the service layer without HTTP, auth, rate limiting or connection pooling.

## What is measured

`make load-test` (script: `benchmarks/scripts/load_test.py`):

1. Restarts the Compose API and embedding worker with **rate limiting off** and the
   **offline deterministic embedding provider**, so no paid API calls are made and
   the numbers aren't just 429s.
2. Seeds a fresh tenant **through the REST API**: 300 synthetic customers × 3
   properties, plus a second credit-limit version for every third customer
   (1,000 fact versions), then waits for the embedding worker.
3. For each concurrency level (default 1, 8, 32) runs a **closed-loop** test for
   30 s after a 5 s warm-up that is not recorded. Seeded request mix:

| Scenario | Weight | Request |
|---|---|---|
| `search` | 50 % | `POST /organizations/{id}/search`: hybrid temporal retrieval, "What is the \<property\> of customer-NNNNN?" |
| `entity_facts` | 30 % | `GET /organizations/{id}/entities/customer/{ext}/facts`: current facts of one entity |
| `fact_write` | 10 % | `POST /organizations/{id}/facts`: a new version (transaction, outbox event, embedding job, cache invalidation) |
| `health` | 10 % | `GET /health`: floor for the HTTP stack itself |

4. Restores the API with the normal settings.

Recorded per level and scenario: request count, throughput, p50/p95/p99/max/mean
client-side latency, status codes, error rate, **429s counted separately** and
transport errors. The result file goes to `benchmarks/results/load-<timestamp>-<commit>.json`
with the commit, the dirty-tree flag, the runtime and the full configuration.

```bash
make up
make load-test
make load-test LOAD_LEVELS=1,4,16,64 LOAD_DURATION=60 LOAD_ENTITIES=1000
```

## How to read the results

- **Closed loop.** Workers wait for each response, so throughput is an *outcome*, and
  at high concurrency latency rises instead of requests being dropped. This measures
  where the system saturates, not behavior under an open arrival rate.
- **One machine.** The load generator, the API container, PostgreSQL, Redis, Neo4j and
  Kafka share one laptop, so they compete for CPU. The absolute numbers describe that
  setup only, not AWS and not production capacity.
- **One API process.** The Compose API runs a single Uvicorn worker; scaling out is
  more replicas (EKS: `replicas: 2`), not measured here.
- **Caching.** Repeated searches can hit the Redis retrieval cache; writes invalidate a
  tenant's cache generation, so the 10 % write mix keeps the hit rate realistic
  rather than 100 %.
- **Synthetic data**, labelled as such in every result file.

## Results

Only results that were actually produced are recorded here, with their file.

| Date (UTC) | Commit | Levels | Result file |
|---|---|---|---|
| (none yet) | | | run `make load-test` |

## What would change the numbers (not done here)

- Several Uvicorn workers or API replicas; PgBouncer in front of PostgreSQL.
- OpenAI embeddings: query embedding adds a network round trip per uncached search.
- An open-loop generator (e.g. k6 `constant-arrival-rate`) to test a target request rate.
