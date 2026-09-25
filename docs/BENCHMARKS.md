# Benchmarks & Measured Evidence

ContextLedger records engineering evidence from Phase 1 onward, so every claim
in the README or a résumé can be traced to a JSON file in `benchmarks/results/`.

## Rules

1. **Only measured numbers.** No estimated, projected, or "typical" figures.
2. **Percent improvements come from two stored measurements** (before/after) of
   the same benchmark on the same machine and dataset size, never from a guess.
3. **Synthetic data is labelled** "synthetic benchmark dataset" everywhere it
   appears. It is never described as real users, customers or production traffic.
4. **Every result is traceable**: commit SHA, dirty-tree flag, UTC date,
   dataset size, environment, runtime/hardware, configuration, metric, value.
5. **Local ≠ cloud.** A laptop result is reported as a laptop result.

## Result schema

```json
{
  "benchmark": "foundation_metrics",
  "commit_sha": "…",
  "working_tree_dirty": false,
  "date": "2026-09-22T23:59:00+00:00",
  "dataset_size": null,
  "environment": "local",
  "runtime": {"python": "3.12.x", "platform": "…", "machine": "arm64"},
  "configuration": {},
  "metrics": {"metric_name": 123}
}
```

## Metrics by phase

| Phase | Metric | Why it matters |
|-------|--------|----------------|
| 1 | pytest test count, pytest duration, API image size | Baseline for test growth and image hardening (Phase 21) |
| 7 | embedding insert throughput; HNSW vs IVFFlat build time and recall | Justifies the index choice |
| 8 | P50/P95/P99 hybrid retrieval latency at 10K / 100K fact versions | Core RAG performance claim |
| 14 | cached vs uncached retrieval latency, cache hit rate | Justifies Redis |
| 15 | Kafka consumer throughput, duplicate-delivery handling | Event pipeline evidence |
| 18 | Recall@K, Precision@K, MRR, temporal correctness %, authorization correctness % | Retrieval quality |
| 21 | CI duration, final image size | DevOps evidence |
| 27 | HTTP p50/p95/p99 latency, throughput and error rate per scenario at several concurrency levels | End-to-end API behavior under load (docs/PERFORMANCE.md) |
| 29 | Terraform apply / destroy duration | AWS demo lifecycle evidence |

## Results so far

| Date (UTC) | Commit | Metric | Value | Environment | File |
|---|---|---|---|---|---|
| 2026-09-23 | `25dfc1a` | pytest tests collected | 34 (all passed) | local, macOS arm64, Python 3.12.14 | `benchmarks/results/foundation-20260923T000953Z-25dfc1a0.json` |
| 2026-09-23 | `25dfc1a` | pytest duration | 0.404 s | same | same |
| 2026-09-23 | `25dfc1a` | API image size (`docker image inspect`) | 59.5 MB | same | same |

### Vector index comparison (Phase 7)

Synthetic benchmark dataset: 20,000 clustered random vectors × 1536 dims, 200
queries, top-10 by cosine distance. Local macOS arm64, PostgreSQL 17.11, pgvector
0.8.6, single connection, client-side latency. Run on commit `817ee84` with the
Phase 7 changes uncommitted (`working_tree_dirty: true`). File: `benchmarks/results/vector-index-20260923T012926-20000-817ee848.json`.

| Strategy | Query setting | Build (s) | Index size (MB) | Recall@10 | p50 (ms) | p95 (ms) |
|---|---|---|---|---|---|---|
| exact (seq scan) | – | – | – | 1.000 | 65.75 | 68.34 |
| IVFFlat (lists=141) | probes=1 | 2.19 | 165.0 | 0.724 | 1.08 | 1.47 |
| IVFFlat (lists=141) | probes=10 | 2.19 | 165.0 | 1.000 | 2.19 | 3.32 |
| IVFFlat (lists=141) | probes=20 | 2.19 | 165.0 | 1.000 | 3.33 | 3.94 |
| HNSW (m=16, ef_construction=64) | ef_search=40 | 10.42 | 163.85 | 0.960 | 1.14 | 1.87 |
| HNSW (m=16, ef_construction=64) | ef_search=100 | 10.42 | 163.85 | 0.966 | 1.19 | 1.87 |

**Reading it honestly.** Both indexes are roughly 30–60× faster than an exact
scan at this size. On this dataset, **IVFFlat built after all data was loaded
beat HNSW on recall** (1.000 at probes=10 vs 0.966) and built about 5× faster.
HNSW had the lower p50 at comparable recall and its recall barely moved between
ef_search 40 and 100. The dataset is only 64 well-separated clusters, which
suits IVFFlat's k-means lists. This benchmark does not test the scenario behind
ADR-020: an index created on an empty table that grows afterwards. That is
measured next, not assumed.

### Hybrid retrieval latency (Phase 8)

Synthetic benchmark dataset (seeded entities, facts and multi-version histories),
offline `deterministic:hash-v1` embeddings. End-to-end `RetrievalService.search`
(permission checks, query embedding, one hybrid SQL statement, fusion,
hydration), single client, sequential, 300 queries after 20 warm-up queries,
70 % "now" and 30 % historical `valid_at`. Local macOS arm64, PostgreSQL 17 +
pgvector 0.8.6 in Docker. Run on commit `1cf1d99` with the Phase 8 changes
uncommitted.

| Fact versions | p50 (ms) | p95 (ms) | p99 (ms) | Mean (ms) | Embed + HNSW build | Sanity: target in top-10 | File |
|---|---|---|---|---|---|---|---|
| 10,000 | 22.20 | 26.32 | 32.81 | 21.76 | 4.4 s | 97 / 117 | `benchmarks/results/retrieval-20260923T134834-10000-1cf1d99c.json` |
| 100,000 | 56.78 | 81.37 | 93.18 | 56.32 | 91.2 s | 113 / 126 | `benchmarks/results/retrieval-20260923T135034-100000-1cf1d99c.json` |

10× the data cost about 2.6× the median latency. The "target in top-10" column is
a sanity check on synthetic questions ("What is the <property> of <entity>?"), not
a retrieval-quality metric. Quality is measured in Phase 18. Not measured here:
concurrent clients (Phase 27) and real OpenAI embeddings, whose API round trip
adds network latency on top of these numbers.

