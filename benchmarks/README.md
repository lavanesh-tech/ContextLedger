# Benchmarks

Reproducible performance and engineering measurements for ContextLedger.
Methodology and honesty rules are in [`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md).

| Path | Purpose |
|------|---------|
| `scripts/` | Benchmark runners. Each writes one JSON file to `results/`. |
| `results/` | Committed JSON results, named `<benchmark>-<UTC timestamp>-<short sha>.json`. |

Current runners:

| Command | Measures | Since |
|---------|----------|-------|
| `make metrics` | pytest test count, pytest duration, API image size | Phase 1 |
