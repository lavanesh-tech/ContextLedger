# Embeddings and Vector Indexing

Phase 7 gives every fact version a vector so that Phase 8 can combine semantic
search with the temporal, tenant and privacy filters that already exist.

## What gets embedded

Each fact version is rendered to a short, stable document (`fact-text-v1`):

```text
customer customer-991
billing / credit limit: 5000
```

`render_fact_document` (`app/domain/embeddings.py`) is pure and deterministic:
JSON values are serialised with sorted keys, so `{"b":1,"a":2}` and
`{"a":2,"b":1}` render identically. The SHA-256 of the text is stored with the
vector. If a tenant already has a vector for the same text and model, it is
reused (for example a limit that goes 2000 → 5000 → 2000 costs one API call, not
two). The cache is per tenant: vectors never cross organizations.

Valid time and source are **not** part of the text. They are filters, not
semantics. Phase 8 applies them in SQL next to the vector distance.

## Tables

| Table | Key | Purpose |
|---|---|---|
| `fact_embeddings` | `(fact_version_id, model)` | the vector (`vector(1536)`), text template, content hash; immutable per model |
| `embedding_jobs` | `id`, `UNIQUE(fact_version_id, model)` | queue: PENDING → RUNNING → SUCCEEDED / FAILED |

Both have `organization_id` with composite foreign keys to `fact_versions`, so
an embedding can never be attached to another tenant's fact. Keying by `model`
lets two models coexist during a migration (for example re-embedding with a
new model) without downtime.

## The worker

```text
enqueue_missing ── INSERT … SELECT versions without a job … ON CONFLICT DO NOTHING
      │
claim ──────────── SELECT … FOR UPDATE SKIP LOCKED  (PENDING and due, or RUNNING with an expired lease)
      │            → RUNNING, attempts + 1, new lease token
      │
render + hash ──── reuse tenant vectors with the same hash
      │
provider.embed ─── outside any transaction (a slow API never holds row locks)
      │
save + complete ── only if the lease token still matches (a late, crashed worker cannot overwrite)
      │
failure ────────── PENDING again after min(cap, base·2^(attempts-1)); FAILED after max attempts
```

* `python -m app.workers.embeddings` runs forever (Docker service `worker`).
  `--once` processes one batch (`make worker-once`).
* Several workers can run at the same time: `SKIP LOCKED` hands each one a
  disjoint set of jobs. A test runs three concurrent workers and asserts every
  job was attempted exactly once.
* A worker that dies mid-batch leaves RUNNING jobs. After `embedding_lease_seconds`
  another worker reclaims them. A CHECK constraint makes "RUNNING without a
  lease" impossible.
* An unexpected error in one iteration (for example PostgreSQL restarting) is
  logged and retried after a pause instead of killing the process. The worker
  touches a heartbeat file only after healthy iterations, and the Compose health
  check and `make smoke` read it.
* The queue is reconciled from the source of truth (`fact_versions` without a
  job), so nothing is lost if a write happens while no worker runs. Phase 15 adds
  Kafka events to trigger work sooner. The reconciliation stays as the safety net.

## Providers

| Provider | `model_id` | When |
|---|---|---|
| `DeterministicHashEmbeddingProvider` | `deterministic:hash-v1` | local development, tests, CI (default). Offline, free, repeatable; captures word overlap only, not meaning |
| `OpenAIEmbeddingProvider` | `openai:text-embedding-3-small` | staging and production (enforced by settings validation) |

The OpenAI adapter talks HTTP directly via `httpx`: bounded retries on
408/409/429/5xx and transport errors, `Retry-After` honoured, jittered
exponential backoff, strict response validation (count, order by `index`,
dimensions), and error messages that never contain the API key. Unit tests
exercise it with `httpx.MockTransport`. **No test calls the real API.** To use
it locally, set `CONTEXTLEDGER_EMBEDDING_PROVIDER=openai` and
`CONTEXTLEDGER_OPENAI_API_KEY` in `.env`. That costs money per token.

## Index: HNSW vs IVFFlat

The production index is HNSW on cosine distance:

```sql
CREATE INDEX ann_fact_embeddings_embedding_cosine
  ON fact_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

| | IVFFlat | HNSW |
|---|---|---|
| Structure | k-means lists; search visits `probes` lists | layered proximity graph |
| Needs data before building | yes, lists are trained on existing rows; quality drops as data drifts | no, works from an empty table |
| Build time / memory | lower | higher |
| Recall/latency trade-off | `ivfflat.probes` | `hnsw.ef_search` |
| Inserts | cheap, but centroids go stale | more expensive, quality stays stable |

ContextLedger starts empty and grows continuously, which is IVFFlat's weak
spot, so HNSW is the default (ADR-020). The benchmark below checks that choice
with measurements instead of assuming it.

### Running the benchmark

```bash
make up
make bench-vector                 # 20,000 × 1536 synthetic vectors
make bench-vector BENCH_ROWS=5000 # quicker
```

`benchmarks/scripts/vector_index_benchmark.py` creates a throwaway database
`contextledger_bench` and generates a **synthetic benchmark dataset** (clustered
random vectors, fixed seed) inside PostgreSQL. It computes exact top-10 with a
sequential scan as ground truth, then builds each index and measures:

* build time and index size;
* recall@10 against the exact results;
* p50 / p95 / mean client-side query latency (single connection, after a warm-up).

IVFFlat uses `lists = √rows` with `probes` 1 / 10 / 20. HNSW uses `m=16`,
`ef_construction=64` with `ef_search` 40 / 100. Results are written to
`benchmarks/results/vector-index-*.json` with the commit SHA and environment,
and summarised in [BENCHMARKS.md](BENCHMARKS.md).

First measured run (details in [BENCHMARKS.md](BENCHMARKS.md#vector-index-comparison-phase-7)):
on 20,000 synthetic vectors, IVFFlat built on the full dataset reached recall@10
1.000 at probes=10 (p50 2.19 ms). HNSW reached 0.966 at ef_search=100
(p50 1.19 ms), with 5× longer builds. The exact scan took about 66 ms. So a
static, pre-loaded dataset favours IVFFlat. HNSW stays the default for the reason
it was chosen: an index that is created on an empty table and grows. That claim
still needs its own measurement (build IVFFlat on a small prefix, insert the
rest, re-measure recall).

Limits of this benchmark: synthetic vectors are not real embeddings, and it
uses one machine and one connection with no concurrent writes. Retrieval
quality on real data is measured separately in Phase 18. Latency under load is
measured in Phase 27.
