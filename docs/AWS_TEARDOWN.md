# AWS teardown

Goal: after a demo, remove everything that is billed by the hour, and know
exactly what is left.

## Destroy the core stack

```bash
make tf-destroy
```

With the default variables:

| Resource | On destroy |
|---|---|
| RDS instance | Deleted; a **final snapshot** `contextledger-demo-final` is kept (set `db_skip_final_snapshot = true` to skip it) |
| NAT gateway, Elastic IP (if enabled) | Deleted |
| Evidence bucket, ECR repositories | Deleted even if not empty (`force_destroy_storage = true`) |
| Secrets | Deleted immediately (`secret_recovery_window_days = 0`), so a later apply can reuse the names |
| RDS-managed master secret | Deleted with the instance |
| VPC, subnets, security groups, endpoints, roles, log groups, alarms, SNS topic, budget | Deleted |

## What remains after destroy

| Item | Why | Cost |
|---|---|---|
| Final RDS snapshot | Lets you restore the data | Snapshot storage (delete it when not needed) |
| Terraform state bucket (bootstrap) | Holds state for the next apply | Small S3 storage |
| RDS log group created by AWS if logs arrived after Terraform deleted its group | AWS recreates it | Small log storage |

Remove the leftovers when you no longer need them:

```bash
aws rds delete-db-snapshot --db-snapshot-identifier contextledger-demo-final
aws logs describe-log-groups --log-group-name-prefix /aws/rds/instance/contextledger
```

## Verify that nothing billable is left

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=contextledger \
  --query 'ResourceTagMappingList[].ResourceARN'
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier, `contextledger`)].DBInstanceIdentifier'
aws ec2 describe-nat-gateways --filter Name=tag:Project,Values=contextledger Name=state,Values=available
aws ec2 describe-addresses --filters Name=tag:Project,Values=contextledger
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
```

The tag query may still list the final snapshot and recently deleted items for
a while; the other commands should return empty lists.

## Recreate later

```bash
make tf-plan
make tf-apply
```

To restore data from the final snapshot instead of starting empty, create the
instance from the snapshot (`snapshot_identifier` on `aws_db_instance`) before
applying; this is a manual, deliberate step.
