# Terraform

Two stacks, applied in order:

| Stack | Creates | Lifetime |
|---|---|---|
| `bootstrap/` | S3 bucket for Terraform state (versioned, encrypted, TLS-only) | Created once, kept |
| `core/` | VPC, RDS PostgreSQL, S3 evidence bucket, ECR, Secrets Manager secrets, IAM roles, CloudWatch logs/alarms, AWS Budget | Created for a demo, destroyed after |

Guides: [AWS deployment](../../docs/AWS_DEPLOYMENT.md),
[teardown](../../docs/AWS_TEARDOWN.md), [cost control](../../docs/AWS_COST_CONTROL.md).
