#!/usr/bin/env bash
# Smoke-test the local Docker Compose stack: every service must really answer,
# not just report "running". Run after `make up`.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a

API_URL="http://127.0.0.1:${API_PORT:-8000}"
failures=0

check() {
  local name="$1"; shift
  if output="$("$@" 2>&1)"; then
    printf '  PASS  %-10s %s\n' "$name" "$(echo "$output" | tail -n 1)"
  else
    printf '  FAIL  %-10s %s\n' "$name" "$(echo "$output" | tail -n 1)"
    failures=$((failures + 1))
  fi
}

echo "ContextLedger local stack smoke test"

check api curl -fsS "$API_URL/api/v1/health"

check ready curl -fsS "$API_URL/api/v1/health/ready"

check schema docker compose exec -T postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  "SELECT 'alembic revision ' || version_num FROM alembic_version"

check postgres docker compose exec -T postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  "SELECT 'pgvector ' || extversion FROM pg_extension WHERE extname = 'vector'"

check pgvector docker compose exec -T postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
  "SELECT 'cosine distance = ' || ('[1,0]'::vector <=> '[0,1]'::vector)"

check redis docker compose exec -T redis redis-cli ping

check neo4j docker compose exec -T neo4j \
  cypher-shell -u neo4j -p "${NEO4J_PASSWORD}" --format plain "RETURN 'neo4j ok' AS status"

check kafka docker compose exec -T kafka \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:29092 --list

if (( failures > 0 )); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "All checks passed"
