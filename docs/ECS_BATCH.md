# ECS/Fargate batch workload

Phase 24 runs one real batch job on AWS Fargate: the **embedding backfill**. It is
code, not a running environment; nothing is applied by default.

## Why this job

Fact versions are embedded asynchronously from a job queue in PostgreSQL. The
long-running embedding worker (Compose locally, a Deployment on EKS) handles the steady
trickle. A backfill is different: after a bulk import, a model change or a period with
no worker running, many jobs are pending at once. That work is finite, so it fits a task
that starts, drains the queue and exits, and is billed only while it runs. The queue
already uses leases (`FOR UPDATE SKIP LOCKED`), so the backfill and a running worker can
work side by side without processing the same job twice.

```bash
python -m app.workers.embeddings --drain 200   # locally: make batch-drain
```

`--drain N` processes batches until nothing is claimable or N batches are done, logs one
`embedding.drain_finished` line with the totals, and exits **1 if any job failed
permanently**, so ECS (and the alert below) treat that run as failed.

## What Terraform creates (`infrastructure/terraform/batch`)

| Resource | Notes |
|---|---|
| ECS cluster | Container Insights off (cost). The cluster itself is free. |
| Task definition | Fargate, ARM64, 0.25 vCPU / 512 MiB, the API image, non-root 10001, read-only root FS, all capabilities dropped. |
| Execution role | Pull from ECR, write logs, read **only** the RDS master secret and the OpenAI key secret. |
| Task role | The core stack's runtime role (already trusted by `ecs-tasks.amazonaws.com`). |
| Security group | Egress only: 443 and 5432 to the database security group. No ingress. A matching rule lets it into RDS. |
| Log group | `/ecs/<name>/embedding-backfill`, 14-day retention. |
| EventBridge Scheduler | Daily cron, created **disabled** (`schedule_enabled = false`). |
| EventBridge rule | Task stopped with a non-zero exit code: publish to the core alerts SNS topic. |

Networking: with the core NAT gateway enabled the task runs in private subnets.
Without it (the cheap default), it runs in public subnets with a public IP used only for
outbound calls. Its security group has no ingress rules, so nothing can connect to it.

## `process_role = batch`

In staging/production the settings refuse to start without a JWT key, Redis, Neo4j
and a metrics token, because the API needs them. The backfill needs none of these, and
feeding it placeholders would weaken the checks. `CONTEXTLEDGER_PROCESS_ROLE=batch`
relaxes only those serving-time requirements. The database password, real
(OpenAI) embeddings and the ban on header auth are still enforced. Unit tests cover all
three cases.

Consequence: the batch task cannot invalidate the API's Redis retrieval cache. Cached
retrieval results for a tenant stay at most `CONTEXTLEDGER_RETRIEVAL_CACHE_TTL_SECONDS`
(default 60 s) stale after a backfill; the long-running worker still invalidates normally.

## Run it

```bash
cp infrastructure/terraform/batch/backend.hcl.example infrastructure/terraform/batch/backend.hcl
cp infrastructure/terraform/batch/terraform.tfvars.example infrastructure/terraform/batch/terraform.tfvars
# the image IMAGE_TAG (default: current commit) must already be in ECR
make batch-plan && make batch-apply
make batch-run        # run-task, wait until stopped, print exit code and reason
aws logs tail /ecs/contextledger-demo/embedding-backfill --since 1h
```

The schema must be migrated first (EKS migration Job, or any `alembic upgrade head`
against RDS). The OpenAI key secret must have a value, or the task exits at startup.

## Cost

No charge while idle. A run costs Fargate vCPU and memory seconds plus the embedding
API tokens it uses. No figure is given here: it depends on how much is pending and was
not measured. The AWS Budget from the core stack still applies.

## Limitations

- Not run against real AWS yet; validated with `terraform validate` and unit tests only.
- One task at a time; parallel tasks would work (leases) but are not configured.
- No retry by the scheduler (`maximum_retry_attempts = 0`): failed jobs are retried by
  the queue's own backoff on the next run.
- Same RDS TLS caveat as EKS: TLS is enforced server-side, the CA is not verified yet.

## Teardown

`make batch-destroy` before destroying the core stack (see AWS_TEARDOWN.md).
