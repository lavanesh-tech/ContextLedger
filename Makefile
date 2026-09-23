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

.PHONY: help install lint format typecheck test test-unit check run \
        migrate migration migrate-check migrate-docker \
        require-env up down down-volumes logs ps smoke docker-build metrics clean \
        worker worker-once bench-vector bench-retrieval

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- Python ---------------------------------------------------------------------
install: ## Create backend/.venv and install the backend with dev tools
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e "$(BACKEND)[dev]"

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-fix lint issues and format code
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: ## mypy --strict
	cd $(BACKEND) && $(BIN)/mypy

test: ## All tests with coverage (database tests need `make up` running)
	cd $(BACKEND) && CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/pytest --cov --cov-report=term-missing

test-unit: ## Only tests that need no database
	cd $(BACKEND) && $(BIN)/pytest -m "not integration"

check: lint typecheck test ## Everything CI runs, locally

run: ## Run the API on your Mac with auto-reload on http://127.0.0.1:8000
	cd $(BACKEND) && $(BIN)/uvicorn app.main:create_app --factory --reload --no-access-log --port 8000

worker: ## Run the embedding worker on your Mac (loop; Ctrl+C to stop)
	cd $(BACKEND) && $(BIN)/python -m app.workers.embeddings

worker-once: ## Embed one batch of pending fact versions and exit
	cd $(BACKEND) && $(BIN)/python -m app.workers.embeddings --once

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
	docker compose up -d --wait api worker

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

docker-build: ## Build the API image on its own
	docker build -t contextledger-api:local $(BACKEND)

# --- Benchmarks -----------------------------------------------------------------
metrics: ## Record foundation metrics (test count, image size) to benchmarks/results/
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/collect_foundation_metrics.py

BENCH_ROWS ?= 20000
bench-vector: ## HNSW vs IVFFlat vs exact search on a synthetic dataset (needs `make up`)
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/vector_index_benchmark.py --rows $(BENCH_ROWS)

BENCH_FACT_VERSIONS ?= 10000
bench-retrieval: ## Hybrid retrieval latency on a synthetic dataset (needs `make up`)
	CONTEXTLEDGER_TEST_DATABASE_URL='$(TEST_DATABASE_URL)' $(BIN)/python benchmarks/scripts/retrieval_benchmark.py --fact-versions $(BENCH_FACT_VERSIONS)

clean: ## Remove caches (not the virtualenv)
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -f $(BACKEND)/.coverage
