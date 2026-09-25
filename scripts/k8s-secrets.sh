#!/usr/bin/env bash
# Build the contextledger-secrets Kubernetes Secret from AWS Secrets Manager.
# Values are piped straight into kubectl; nothing is written to disk or printed.
# Redis and Neo4j passwords are generated per cluster (both are in-cluster, ephemeral).
set -euo pipefail

PROJECT="${PROJECT:-contextledger}"
ENVIRONMENT="${ENVIRONMENT:-staging}"
NAMESPACE="${NAMESPACE:-contextledger}"
TF_CORE="${TF_CORE:-infrastructure/terraform/core}"

get() { aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text; }
optional() { get "$1" 2>/dev/null || true; }

db_json="$(get "$(terraform -chdir="$TF_CORE" output -raw db_master_secret_arn)")"
db_user="$(printf '%s' "$db_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')"
db_pass="$(printf '%s' "$db_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
jwt_key="$(get "$PROJECT/$ENVIRONMENT/jwt-signing-key")"
metrics="$(get "$PROJECT/$ENVIRONMENT/metrics-token")"
openai="$(optional "$PROJECT/$ENVIRONMENT/openai-api-key")"
redis_pass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
neo4j_pass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
llm_provider="disabled"; [ -n "$openai" ] && llm_provider="openai"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" create secret generic contextledger-secrets \
  --from-literal=CONTEXTLEDGER_DB_USER="$db_user" \
  --from-literal=CONTEXTLEDGER_DB_PASSWORD="$db_pass" \
  --from-literal=CONTEXTLEDGER_JWT_SIGNING_KEY="$jwt_key" \
  --from-literal=CONTEXTLEDGER_METRICS_TOKEN="$metrics" \
  --from-literal=CONTEXTLEDGER_OPENAI_API_KEY="$openai" \
  --from-literal=CONTEXTLEDGER_LLM_PROVIDER="$llm_provider" \
  --from-literal=CONTEXTLEDGER_REDIS_URL="redis://:${redis_pass}@redis:6379/0" \
  --from-literal=REDIS_PASSWORD="$redis_pass" \
  --from-literal=CONTEXTLEDGER_NEO4J_PASSWORD="$neo4j_pass" \
  --from-literal=NEO4J_AUTH="neo4j/${neo4j_pass}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secret contextledger-secrets applied in namespace $NAMESPACE"

# Public AWS RDS CA bundle, so the app verifies the database certificate (verify-full).
ca="$(mktemp)"; trap 'rm -f "$ca"' EXIT
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o "$ca"
grep -q "BEGIN CERTIFICATE" "$ca"
kubectl -n "$NAMESPACE" create configmap rds-ca --from-file=global-bundle.pem="$ca" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "configmap rds-ca applied in namespace $NAMESPACE"
