# Athena analytics over evaluation results

Phase 26. Athena is used for one thing this project actually produces: **retrieval
evaluation results** (Phase 18). It is code, not a running environment; nothing is
applied by default.

## Why Athena here and not elsewhere

Evaluation runs are append-only JSON files committed under `evaluation/results/`. Comparing
runs across commits and configurations is an analytical, read-only question over files,
which is what Athena is for. Putting them in PostgreSQL would mix offline benchmark data
into the transactional system of record.

The spec also suggested "which sources produced the most contradictions?" and "which
agents had the most context-resolution failures?". Those questions are about **live
tenant data** in PostgreSQL. Exporting tenant data to S3 would need a tenant-aware export
pipeline, retention rules and access control, so it is out of scope. Today they are
answered with SQL against PostgreSQL through the API (`/contradictions`, the
`agent_runs` table), where tenant isolation is already enforced.

## Data flow

```
evaluation/results/retrieval-*.json            (committed, produced by make eval-retrieval)
      │  make analytics-export                 app/evaluation/analytics_export.py (unit-tested)
      ▼
evaluation/analytics/                          (git-ignored, derived)
  retrieval_runs/run_date=YYYY-MM-DD/<run_id>.jsonl   one row per configuration
  retrieval_cases/run_date=YYYY-MM-DD/<run_id>.jsonl  one row per case and configuration
      │  make analytics-upload                 aws s3 sync --delete
      ▼
s3://<name>-analytics-<account>/retrieval/…    Glue tables, partition projection on run_date
      │
      ▼
Athena workgroup (enforced settings, 100 MiB scan limit per query, SSE-S3 results kept 7 days)
```

The export only restates what a result file contains: metric names become SQL-friendly
columns (`recall@5` becomes `recall_at_5`) and ranks come from the recorded
`ranked_keys`. Every row carries `synthetic` and `working_tree_dirty`, so queries can't
silently mix synthetic benchmarks with anything else.

## Named queries

| Name | Question |
|---|---|
| `best-recall-at-5` | Which retrieval configuration produced the best Recall@5? |
| `mrr-by-category` | MRR and median latency per case category and configuration |
| `correctness-violations` | Cases that returned forbidden (superseded, revoked, other-tenant, private) facts |
| `trend-by-commit` | MRR and Recall@5 across commits |

```bash
cp infrastructure/terraform/analytics/backend.hcl.example infrastructure/terraform/analytics/backend.hcl
cp infrastructure/terraform/analytics/terraform.tfvars.example infrastructure/terraform/analytics/terraform.tfvars
make analytics-plan && make analytics-apply
make analytics-upload
make analytics-query Q=best-recall-at-5
```

The analytics stack does not depend on core, EKS or batch; it can exist on its own.

## Honest limits

- The data is small and SYNTHETIC. On the current dataset the three configurations
  score the same (see evaluation/README.md). Athena adds query convenience, not insight
  the JSON files don't already hold.
- The export and table definitions are unit-tested and validated with `terraform
  validate`, but they have not been queried on real AWS yet.
- Grounded-answer evaluation results are not exported yet (different shape; a
  follow-up would add an `answer_runs` table).

## Cost

S3 storage for a few kilobytes, plus Athena's per-TB-scanned price for each query,
with a 100 MiB cap per query. No exact figure is given because it was not measured.
`make analytics-destroy` removes everything, including the bucket contents, which are
re-exportable.
