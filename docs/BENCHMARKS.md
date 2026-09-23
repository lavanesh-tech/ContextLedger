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
| 29 | Terraform apply / destroy duration | AWS demo lifecycle evidence |

## Results so far

| Date (UTC) | Commit | Metric | Value | Environment | File |
|---|---|---|---|---|---|
| 2026-09-23 | `25dfc1a` | pytest tests collected | 34 (all passed) | local, macOS arm64, Python 3.12.14 | `benchmarks/results/foundation-20260923T000953Z-25dfc1a0.json` |
| 2026-09-23 | `25dfc1a` | pytest duration | 0.404 s | same | same |
| 2026-09-23 | `25dfc1a` | API image size (`docker image inspect`) | 59.5 MB | same | same |
