# EKS deployment

Phase 23 adds an optional Kubernetes deployment on Amazon EKS. It is **code, not a
running environment**: nothing here has been applied to AWS by default, and an EKS
cluster is billable for every hour it exists (control plane, nodes, NAT gateway,
load of the RDS instance from the core stack). Create it for a demo, then destroy it.

## Layout

```
infrastructure/terraform/eks/        cluster, node group, add-ons, Pod Identity, DB access
infrastructure/kubernetes/base/      namespace, config, API, workers, web, Redis, Neo4j, NetworkPolicies
infrastructure/kubernetes/overlays/eks/   image references + RDS host (set by make k8s-render)
infrastructure/kubernetes/jobs/      Alembic migration Job (applied separately, before rollout)
scripts/k8s-secrets.sh               Secrets Manager -> Kubernetes Secret
```

## What runs where

| Component | Where | Notes |
|---|---|---|
| PostgreSQL + pgvector | RDS (core stack) | System of record. Reached from the cluster security group only. |
| API (2 replicas), embedding worker, graph projector | EKS pods | Same image; non-root UID 10001, read-only root FS, all capabilities dropped. |
| Web (Next.js standalone) | EKS pod | Talks to the API Service in-cluster. |
| Redis | EKS pod, no persistence | Rebuildable state only (cache, rate limits, idempotency). |
| Neo4j | EKS StatefulSet, emptyDir | A projection of PostgreSQL; rebuilt by the graph projector. Demo only. |
| Kafka, event relay, event consumers | **not deployed** | Events stay in the PostgreSQL outbox; the relay can be added later (e.g. MSK) without code changes. |

## Security choices

- **Private nodes.** Nodes run in private subnets; the cluster needs the core stack's NAT
  gateway (`enable_nat_gateway = true`), enforced by a Terraform `check` block.
- **API endpoint** is public but restricted to `api_allowed_cidrs` (your IP).
- **Access entries** (API authentication mode) instead of the `aws-auth` ConfigMap.
- **EKS Pod Identity** binds the `contextledger-app` service account to the core stack's
  least-privilege runtime role. No static AWS keys in pods, no node-role over-reach.
- **Pod Security Admission `restricted`** on the namespace; every pod sets
  `runAsNonRoot`, `seccompProfile: RuntimeDefault`, drops all capabilities.
- **NetworkPolicies** (default-deny ingress, then web->API, app->Redis, app->Neo4j),
  enforced by the VPC CNI network-policy agent.
- **Secrets** live in AWS Secrets Manager; `make k8s-secrets` copies them into one
  Kubernetes Secret without writing them to disk or printing them. The OpenAI key is
  optional: without it the LLM provider is `disabled`.
- Control-plane logs (api, audit, authenticator) go to CloudWatch with 7-day retention.

## Deploy

```bash
# core stack with the NAT gateway enabled (see AWS_DEPLOYMENT.md)
cp infrastructure/terraform/eks/backend.hcl.example infrastructure/terraform/eks/backend.hcl
cp infrastructure/terraform/eks/terraform.tfvars.example infrastructure/terraform/eks/terraform.tfvars
make eks-plan && make eks-apply
aws eks update-kubeconfig --name <cluster_name> --region <region>
# push images tagged with the current commit to ECR (release workflow or docker push)
make k8s-secrets
make k8s-deploy
kubectl -n contextledger port-forward svc/web 3000:3000
```

No public load balancer is created: access is via `kubectl port-forward`, which keeps
the demo off the internet. An Ingress with the AWS Load Balancer Controller and TLS
would be the next step for a shared environment.

## Known limitations (honest list)

- Redis and Neo4j are ephemeral single pods; losing them loses only rebuildable state.
- Database TLS: RDS enforces TLS (`rds.force_ssl`), but the client does not yet verify
  the RDS CA certificate. Follow-up: ship the RDS CA bundle and use `sslmode=verify-full`.
- No HorizontalPodAutoscaler, no Cluster Autoscaler/Karpenter: node count is fixed.
- The Spot node group can be interrupted; the API has 2 replicas and a PodDisruptionBudget.
- Not load-tested (that is Phase 27).

## Validation without AWS

```bash
make tf-check        # terraform fmt + validate for bootstrap, core and eks
make k8s-validate    # kustomize build | kubeconform -strict against Kubernetes 1.33 schemas
```

Both run in CI without AWS credentials.

## Teardown

Always destroy EKS **before** core: `make eks-destroy`, then the core stack. See
[AWS_TEARDOWN.md](AWS_TEARDOWN.md).
