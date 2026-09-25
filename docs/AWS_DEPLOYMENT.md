# AWS deployment: core infrastructure (Phase 22)

Terraform in `infrastructure/terraform/` creates the AWS foundations. Nothing is
created until you run `apply` with your own AWS credentials, and **applying
creates resources that are billed** (see [AWS_COST_CONTROL.md](AWS_COST_CONTROL.md)).
Workloads (EKS, ECS) come in Phases 23–24; this phase creates what they will use.

## What the core stack creates

| Area | Resources | Notes |
|---|---|---|
| Network | VPC `10.40.0.0/16`, 2 public + 2 private subnets in 2 AZs, internet gateway, S3 gateway endpoint, app security group | No NAT gateway unless `enable_nat_gateway = true` |
| Database | RDS PostgreSQL 17 `db.t4g.micro`, 20 GB gp3, single-AZ | Private subnets only, encrypted, `rds.force_ssl=1`, master password generated and kept by RDS in Secrets Manager, ingress only from the app security group |
| Storage | S3 evidence bucket | Private (public access blocked, bucket-owner enforced), versioned, encrypted, TLS-only policy, old versions expire after 30 days |
| Registry | ECR `contextledger-api`, `contextledger-web` | Immutable tags, scan on push, untagged images expire after 7 days, at most 10 images kept |
| Secrets | `contextledger/<env>/jwt-signing-key`, `openai-api-key`, `metrics-token` | Containers only; values are set with the CLI, never in Terraform state |
| IAM | App runtime role (EKS Pod Identity / ECS tasks), GitHub OIDC provider and CI role | Runtime: its own secrets, evidence objects, its log groups. CI: push to the two ECR repositories, only from `main` and `v*` tags. No long-lived AWS keys |
| Monitoring | Log groups (14-day retention), SNS alert topic with email, RDS alarms (CPU, free storage, connections), monthly AWS Budget | Budget alerts at 50/80/100% actual and 100% forecast |

Every resource carries the tags `Project`, `Environment`, `ManagedBy` and `Owner`.

## Prerequisites

- An AWS account and a user or SSO role allowed to create these resources.
- AWS CLI v2, configured (`aws sts get-caller-identity` works).
- Terraform 1.10 or newer (`brew install hashicorp/tap/terraform`).

## 1. Bootstrap remote state (once per account and region)

```bash
cd infrastructure/terraform/bootstrap
terraform init
terraform apply
```

This creates a versioned, encrypted S3 bucket for Terraform state. State
locking uses S3 lock files (`use_lockfile`), so no DynamoDB table is needed.

## 2. Configure the core stack

```bash
cd ../core
cp backend.hcl.example backend.hcl           # set bucket to the bootstrap output
cp terraform.tfvars.example terraform.tfvars # set owner and alert_email
```

Both files are git-ignored.

## 3. Plan and apply

```bash
make tf-plan     # from the repository root: init + plan, saved to core.tfplan
make tf-apply    # applies exactly that plan
```

Then confirm the SNS subscription email so alarms and budget alerts reach you.

## 4. Set the application secrets

```bash
backend/.venv/bin/python -m app.auth.tokens | aws secretsmanager put-secret-value \
  --secret-id contextledger/demo/jwt-signing-key --secret-string file:///dev/stdin
aws secretsmanager put-secret-value --secret-id contextledger/demo/metrics-token \
  --secret-string "$(openssl rand -hex 32)"
aws secretsmanager put-secret-value --secret-id contextledger/demo/openai-api-key \
  --secret-string "<your key>"   # only if you use OpenAI
```

## 5. Push images to ECR (optional until Phase 23)

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker build -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/contextledger-api:v0.22.0 backend
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/contextledger-api:v0.22.0
```

## Verification

```bash
terraform -chdir=infrastructure/terraform/core output
aws rds describe-db-instances --db-instance-identifier contextledger-demo \
  --query 'DBInstances[0].[DBInstanceStatus,PubliclyAccessible,StorageEncrypted]'
aws s3api get-public-access-block --bucket "$(terraform -chdir=infrastructure/terraform/core output -raw evidence_bucket)"
```

Expected: `available`, `false`, `true`, and all four public-access blocks `true`.

## Not in this phase

The database is reachable only from inside the VPC, so migrations and the API
run there in Phase 23 (EKS) or 24 (ECS). No load balancer, DNS or TLS
certificate is created yet.
