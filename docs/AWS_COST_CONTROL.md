# AWS cost control

This is a portfolio/demo deployment: run it for a demo, then tear it down. Even
torn down, the account is **not free**: snapshots, the Terraform state bucket,
logs and similar leftovers cost small amounts. The aim is to keep ongoing
cost low while the project is offline, not zero.

Prices change and differ by region: check the
[AWS Pricing Calculator](https://calculator.aws/) for current numbers rather
than trusting figures in a README.

## What costs money in the core stack

| Resource | Billing | Default choice |
|---|---|---|
| RDS PostgreSQL | Per hour while it exists, plus storage and backups | Smallest Graviton class (`db.t4g.micro`), single-AZ, 20 GB gp3, 1-day backups |
| NAT gateway | Per hour, plus data processed | **Off** (`enable_nat_gateway = false`); an S3 gateway endpoint is free |
| Elastic IP | Public IPv4 addresses are billed | Only created with the NAT gateway |
| S3 (evidence, state) | Storage and requests | Old versions expire; incomplete uploads aborted |
| ECR | Image storage | At most 10 images per repository, untagged expire after 7 days |
| CloudWatch Logs | Ingestion and storage | 14-day retention on every group |
| Secrets Manager | Per secret per month, plus API calls | Three app secrets and one RDS-managed secret |
| RDS snapshots | Storage | One final snapshot on destroy; delete it when not needed |

Phases 23–24 add EKS (a control-plane hourly charge plus nodes), load
balancers and ECS tasks. They follow the same rule: create for a demo, destroy
after, and verify with [AWS_TEARDOWN.md](AWS_TEARDOWN.md).

## Guardrails built into the Terraform

- **AWS Budget** (`monthly_budget_usd`, default 30): email alerts at 50%, 80%
  and 100% of actual spend and at 100% of forecast spend. A budget alerts; it
  does not stop resources.
- **Tags** on every resource: `Project`, `Environment`, `ManagedBy`, `Owner`.
  Activate `Project` and `Environment` as cost allocation tags (Billing →
  Cost allocation tags) to see spend per project in Cost Explorer.
- **Teardown-friendly defaults**: no deletion protection, forced deletion of
  buckets and repositories, immediate secret deletion.
- **CloudWatch alarms** on the database go to the same email.

## Billing review routine

1. After each demo: `make tf-destroy`, then run the verification commands in
   [AWS_TEARDOWN.md](AWS_TEARDOWN.md).
2. Weekly while deployed: Cost Explorer, grouped by service and filtered by
   `Project = contextledger`.
3. Monthly: delete snapshots, images and state versions that are no longer
   needed; check the Free Tier usage page if the account is eligible.
