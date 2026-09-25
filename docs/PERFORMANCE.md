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
| 2026-09-25 | `a9a5aea` | 1, 8, 32 | `load-20260925T023235-a9a5aea7.json`: **invalid for writes**. Every `fact_write` returned 422 because the load generator left out the required `valid_from`, which was a bug in the test, not the API. Reads were valid, but the mix was wrong, so the run is kept only as a record. Fixed in the next commit; the result files now also keep the first error body per scenario and status. |
| 2026-09-25 | `0f7bb54` (fix uncommitted, dirty tree) | 1, 8, 32 | `load-20260925T023654-0f7bb54d.json`: first valid run (below) |

### First valid run (`load-20260925T023654-0f7bb54d.json`)

MacBook Pro (Apple Silicon, macOS 26.6), Docker Compose stack, one API process,
synthetic tenant with 1,000 seeded fact versions, deterministic embeddings, 30 s per
level after a 5 s warm-up. Client-side latency in ms.

| Concurrency | Throughput (req/s) | Requests | Errors | search p50 / p95 / p99 | entity_facts p50 / p95 | fact_write p50 / p95 | health p50 |
|---|---|---|---|---|---|---|---|
| 1 | 57.9 | 1,738 | 0 | 22.3 / 25.2 / 397.4 | 5.0 / 6.1 | 9.0 / 11.7 | 1.6 |
| 8 | 135.6 | 4,067 | 0 | 75.5 / 121.2 / 147.3 | 34.3 / 67.6 | 61.0 / 103.5 | 6.4 |
| 32 | 162.2 | 4,865 | 1 (409) | 267.1 / 359.3 / 413.6 | 123.8 / 192.6 | 222.0 / 312.7 | 16.7 |

What this run shows (and only this):

- **Saturation between 8 and 32 workers.** Going from 8 to 32 workers adds about 20 %
  throughput while search p50 grows about 3.5×, so the single API process on this
  laptop is saturated. More concurrency now adds queueing, not work.
- **Search is the most expensive path**, as expected: an embedding plus one hybrid SQL
  statement, against about 5 ms for an indexed entity read at concurrency 1.
- **Tail at concurrency 1:** search p99 was 397 ms against a p50 of 22 ms. The run
  doesn't show the cause. The likely candidates are cache-miss bursts after writes
  and the embedding worker competing for the database. Not investigated yet.
- **The one 409 is correct behavior:** two concurrent writes to the same fact, where
  the later-committed one had an earlier `valid_from`. The API refuses to rewrite
  history without a revocation (docs/REVOCATION.md) instead of silently reordering.
- With writes succeeding, throughput is lower than in the invalid first run: real
  writes create versions, outbox events and embedding jobs, and invalidate the tenant's
  retrieval cache.

## What would change the numbers (not done here)

- Several Uvicorn workers or API replicas; PgBouncer in front of PostgreSQL.
- OpenAI embeddings: query embedding adds a network round trip per uncached search.
- An open-loop generator (e.g. k6 `constant-arrival-rate`) to test a target request rate.
