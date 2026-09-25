SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3.12
BACKEND := backend
VENV := $(BACKEND)/.venv
BIN := $(abspath $(VENV))/bin

# Read POSTGRES_* from .env (if present) to build the test database URL.
-include .env
POSTGRES_USER ?= contextledger
POSTGRES_PASSWORD ?=
POSTGRES_PORT ?= 5432
TEST_DATABASE_URL ?= postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_PORT)/contextledger_test
NEO4J_PASSWORD ?=
NEO4J_BOLT_PORT ?= 7687
TEST_NEO4J_ENV = CONTEXTLEDGER_TEST_NEO4J_URI='bolt://127.0.0.1:$(NEO4J_BOLT_PORT)' CONTEXTLEDGER_TEST_NEO4J_PASSWORD='$(NEO4J_PASSWORD)'
REDIS_PASSWORD ?=
REDIS_PORT ?= 6379
# Database 15: tests never touch the database the local stack uses (0).
TEST_REDIS_ENV = CONTEXTLEDGER_TEST_REDIS_URL='redis://:$(REDIS_PASSWORD)@127.0.0.1:$(REDIS_PORT)/15'
KAFKA_PORT ?= 9092
TEST_KAFKA_ENV = CONTEXTLEDGER_TEST_KAFKA_BOOTSTRAP_SERVERS='127.0.0.1:$(KAFKA_PORT)'

.PHONY: eks-plan eks-apply eks-destroy k8s-validate k8s-secrets k8s-render k8s-migrate k8s-deploy
.PHONY: help install lock lint format typecheck test test-unit check run \
        migrate migration migrate-check migrate-docker \
        require-env up down down-volumes logs ps smoke docker-build metrics clean \
        worker worker-once graph-projector graph-once event-relay event-consumers kafka-topics mcp api-docs eval eval-live eval-retrieval frontend-install frontend-dev frontend-check obs-up obs-down tf-check tf-plan tf-apply tf-destroy jwt-key bench-vector bench-retrieval

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Python ---------------------------------------------------------------------
install: ## Create backend/.venv and install the backend with dev tools
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install --require-hashes --no-deps -r $(BACKEND)/requirements-dev.lock
	$(BIN)/pip install --no-deps -e "$(BACKEND)[dev]"

lock: ## Re-resolve backend/requirements*.lock from pyproject.toml (needs uv: brew install uv)
	cd $(BACKEND) && uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock -q
	cd $(BACKEND) && uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock -q

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-fix lint issues and format code
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: ## mypy --strict
	cd $(BACKEND) && $(BIN)/mypy

test: ## All tests with coverage (database tests need `make up` running)
	cd $(BACKEND) && CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(TEST_NEO4J_ENV) $(TEST_REDIS_ENV) $(TEST_KAFKA_ENV) $(BIN)/pytest --cov --cov-report=term-missing

test-unit: ## Only tests that need no database
	cd $(BACKEND) && $(BIN)/pytest -m "not integration"

check: lint typecheck test ## Everything CI runs, locally

run: ## Run the API on your Mac with auto-reload on http://127.0.0.1:8000
	cd $(BACKEND) && $(BIN)/uvicorn app.main:create_app --factory --reload --no-access-log --port 8000

worker: ## Run the embedding worker on your Mac (loop; Ctrl+C to stop)
	cd $(BACKEND) && $(BIN)/python -m app.workers.embeddings

worker-once: ## Embed one batch of pending fact versions and exit
	cd $(BACKEND) && $(BIN)/python -m app.workers.embeddings --once

graph-projector: ## Run the Neo4j graph projector on your Mac (loop; Ctrl+C to stop)
	cd $(BACKEND) && CONTEXTLEDGER_NEO4J_PASSWORD='$(NEO4J_PASSWORD)' $(BIN)/python -m app.workers.graph

graph-once: ## Project everything pending into Neo4j and exit
	cd $(BACKEND) && CONTEXTLEDGER_NEO4J_PASSWORD='$(NEO4J_PASSWORD)' $(BIN)/python -m app.workers.graph --once

event-relay: ## Publish the event outbox to Kafka on your Mac (loop; Ctrl+C to stop)
	cd $(BACKEND) && CONTEXTLEDGER_KAFKA_BOOTSTRAP_SERVERS='127.0.0.1:$(KAFKA_PORT)' $(BIN)/python -m app.workers.event_relay

event-consumers: ## Run the Kafka event consumers on your Mac (loop; Ctrl+C to stop)
	cd $(BACKEND) && CONTEXTLEDGER_KAFKA_BOOTSTRAP_SERVERS='127.0.0.1:$(KAFKA_PORT)' $(BIN)/python -m app.workers.event_consumers

kafka-topics: ## Create the event topics and their dead-letter topics (idempotent)
	docker compose run --rm --no-deps event-relay python -c "import asyncio; from app.core.config import get_settings; from app.events.kafka import ensure_topics; print(asyncio.run(ensure_topics(get_settings())))"

mcp: ## Run the MCP server over stdio (needs CONTEXTLEDGER_MCP_ORGANIZATION_ID / _USER_ID)
	cd $(BACKEND) && CONTEXTLEDGER_NEO4J_PASSWORD='$(NEO4J_PASSWORD)' $(BIN)/python -m app.mcp.server

api-docs: ## Regenerate docs/api/openapi.json and the Postman collection
	cd $(BACKEND) && $(BIN)/python -m app.api.docs_export

jwt-key: ## Print a new ES256 signing key (PEM) for CONTEXTLEDGER_JWT_SIGNING_KEY
	@cd $(BACKEND) && $(BIN)/python -m app.auth.tokens

# --- Database migrations --------------------------------------------------------
migrate: ## Apply all migrations to the local database (from your Mac)
	cd $(BACKEND) && $(BIN)/alembic upgrade head

migration: ## New migration: make migration m="create facts table"
	@test -n "$(m)" || (echo 'usage: make migration m="describe the change"' && exit 1)
	cd $(BACKEND) && next=$$(printf "%04d" $$(( $$(ls migrations/versions/[0-9]*.py | wc -l) + 1 ))) && \
	  $(BIN)/alembic revision --autogenerate --rev-id "$$next" -m "$(m)"

migrate-check: ## Fail if ORM models and migrations have drifted apart
	cd $(BACKEND) && $(BIN)/alembic check

migrate-docker: require-env ## Apply migrations using the API image (as a deploy job would)
	docker compose run --rm --no-deps api alembic upgrade head

# --- Docker ---------------------------------------------------------------------
require-env:
	@test -f .env || (echo "No .env found. Run: cp .env.example .env  (then change the passwords)" && exit 1)

up: require-env ## Build, start data stores, run migrations, start the API
	docker compose build api
	docker compose up -d --wait postgres redis neo4j kafka
	docker compose run --rm --no-deps api alembic upgrade head
	docker compose up -d --wait api worker graph-projector event-relay event-consumers

down: ## Stop the stack (keeps data volumes)
	docker compose down

down-volumes: ## Stop the stack AND delete local Postgres/Neo4j data
	docker compose down -v

logs: ## Follow logs of all services
	docker compose logs -f

ps: ## Show service status
	docker compose ps

smoke: require-env ## Verify every running service actually answers
	./scripts/smoke_local_stack.sh

docker-build: ## Build the API and web images
	docker build -t contextledger-api:local $(BACKEND)
	docker build -t contextledger-web:local frontend

# --- Benchmarks -----------------------------------------------------------------
metrics: ## Record foundation metrics (test count, image size) to benchmarks/results/
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/collect_foundation_metrics.py

BENCH_ROWS ?= 20000
bench-vector: ## HNSW vs IVFFlat vs exact search on a synthetic dataset (needs `make up`)
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/vector_index_benchmark.py --rows $(BENCH_ROWS)

BENCH_FACT_VERSIONS ?= 10000
bench-retrieval: ## Hybrid retrieval latency on a synthetic dataset (needs `make up`)
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/retrieval_benchmark.py --fact-versions $(BENCH_FACT_VERSIONS)

# --- Evaluation (see evaluation/README.md) ----------------------------------------
PROMPTS ?= grounded-answer-v1,grounded-answer-v2
LIMIT ?=
EVAL_MODEL ?= gpt-4o-mini
eval: ## Deterministic grounded-answer evaluation (free; pipeline metrics; needs `make up`)
	cd $(BACKEND) && CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python -m app.evaluation.runner --prompts $(PROMPTS) $(if $(LIMIT),--limit $(LIMIT),)

eval-retrieval: ## Retrieval evaluation: Recall@K, Precision@K, MRR, nDCG, temporal/authorization correctness (free; needs `make up`)
	cd $(BACKEND) && CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python -m app.evaluation.retrieval_eval

eval-live: ## LIVE OpenAI evaluation (COSTS MONEY): needs CONTEXTLEDGER_OPENAI_API_KEY in the environment
	cd $(BACKEND) && CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python -m app.evaluation.runner --mode live --live --model $(EVAL_MODEL) --prompts $(PROMPTS) $(if $(LIMIT),--limit $(LIMIT),) $(if $(PRICE_IN),--price-input $(PRICE_IN),) $(if $(PRICE_OUT),--price-output $(PRICE_OUT),)

# --- Observability (see docs/OBSERVABILITY.md) ---------------------------------------------

obs-up: require-env ## Prometheus :9090, Grafana :3001 (admin / GRAFANA_ADMIN_PASSWORD), Jaeger :16686
	docker compose --profile observability up -d prometheus grafana jaeger

obs-down: ## Stop the observability containers
	docker compose --profile observability stop prometheus grafana jaeger

# --- Terraform / AWS (see docs/AWS_DEPLOYMENT.md; apply and destroy cost or save money) ----

TF_CORE := infrastructure/terraform/core
TF_EKS := infrastructure/terraform/eks
K8S := infrastructure/kubernetes
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)

tf-check: ## terraform fmt + validate for both stacks (no AWS calls)
	terraform fmt -check -recursive infrastructure/terraform
	terraform -chdir=infrastructure/terraform/bootstrap init -backend=false -input=false >/dev/null
	terraform -chdir=infrastructure/terraform/bootstrap validate
	terraform -chdir=$(TF_CORE) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_CORE) validate
	terraform -chdir=$(TF_EKS) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_EKS) validate

tf-plan: ## Plan the core AWS stack (needs AWS credentials, backend.hcl and terraform.tfvars)
	terraform -chdir=$(TF_CORE) init -backend-config=backend.hcl -input=false
	terraform -chdir=$(TF_CORE) plan -out=core.tfplan

tf-apply: ## Apply the saved plan (CREATES BILLABLE AWS RESOURCES)
	terraform -chdir=$(TF_CORE) apply core.tfplan

tf-destroy: ## Destroy the core AWS stack (see docs/AWS_TEARDOWN.md for what remains)
	terraform -chdir=$(TF_CORE) destroy

# --- Frontend (Next.js, see frontend/README.md) -------------------------------------------

frontend-install: ## Install the web UI's dependencies from its lock file
	cd frontend && npm ci

frontend-dev: ## Web UI on http://localhost:3000 (forwards /api/v1 to `make run`)
	cd frontend && NEXT_TELEMETRY_DISABLED=1 npm run dev

frontend-check: ## Web UI type-check, unit tests and production build
	cd frontend && NEXT_TELEMETRY_DISABLED=1 npm run check

clean: ## Remove caches (not the virtualenv)
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -f $(BACKEND)/.coverage

eks-plan: ## Plan the EKS stack (needs the core stack applied with enable_nat_gateway=true)
	terraform -chdir=$(TF_EKS) init -backend-config=backend.hcl -input=false
	terraform -chdir=$(TF_EKS) plan -out=tfplan

eks-apply: ## Apply the saved EKS plan (billable: control plane, nodes, NAT)
	terraform -chdir=$(TF_EKS) apply tfplan

eks-destroy: ## Destroy the EKS stack (run before destroying core)
	-kubectl delete namespace contextledger --wait=true --timeout=5m
	terraform -chdir=$(TF_EKS) destroy

k8s-validate: ## Render the Kubernetes manifests and validate them against the 1.33 schemas
	kustomize build $(K8S)/overlays/eks | kubeconform -strict -summary -kubernetes-version 1.33.0 -
	kustomize build $(K8S)/jobs | kubeconform -strict -summary -kubernetes-version 1.33.0 -

k8s-secrets: ## Create/refresh the app Secret from AWS Secrets Manager
	TF_CORE=$(TF_CORE) scripts/k8s-secrets.sh

k8s-render: ## Render the eks overlay with this commit's images and the RDS host into .k8s-rendered/
	rm -rf .k8s-rendered && mkdir -p .k8s-rendered && cp -R $(K8S) .k8s-rendered/k
	cd .k8s-rendered/k/overlays/eks && \
	  kustomize edit set image contextledger-api=$$(terraform -chdir=../../../../$(TF_CORE) output -json ecr_repository_urls | python3 -c 'import json,sys;print(json.load(sys.stdin)["contextledger-api"])'):$(IMAGE_TAG) && \
	  kustomize edit set image contextledger-web=$$(terraform -chdir=../../../../$(TF_CORE) output -json ecr_repository_urls | python3 -c 'import json,sys;print(json.load(sys.stdin)["contextledger-web"])'):$(IMAGE_TAG) && \
	  kustomize edit add configmap contextledger-config --behavior=merge --from-literal=CONTEXTLEDGER_DB_HOST=$$(terraform -chdir=../../../../$(TF_CORE) output -raw db_endpoint)
	cd .k8s-rendered/k/jobs && kustomize edit set image contextledger-api=$$(terraform -chdir=../../../$(TF_CORE) output -json ecr_repository_urls | python3 -c 'import json,sys;print(json.load(sys.stdin)["contextledger-api"])'):$(IMAGE_TAG)

k8s-migrate: k8s-render ## Run Alembic migrations as a one-off Job and wait for it
	-kubectl -n contextledger delete job migrate --ignore-not-found
	kustomize build .k8s-rendered/k/overlays/eks | kubectl apply -f -
	kustomize build .k8s-rendered/k/jobs | kubectl apply -f -
	kubectl -n contextledger wait --for=condition=complete job/migrate --timeout=5m

k8s-deploy: k8s-migrate ## Migrate, then roll out API, workers and web
	kubectl -n contextledger rollout status deployment/api --timeout=5m
	kubectl -n contextledger rollout status deployment/web --timeout=5m
