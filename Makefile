SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3.12
BACKEND := backend
VENV := $(BACKEND)/.venv
BIN := $(abspath $(VENV))/bin

.PHONY: help install lint format typecheck test check run up down down-volumes logs ps smoke docker-build metrics clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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

test: ## Run pytest with coverage
	cd $(BACKEND) && $(BIN)/pytest --cov --cov-report=term-missing

check: lint typecheck test ## Everything CI runs, locally

run: ## Run the API with auto-reload on http://127.0.0.1:8000
	cd $(BACKEND) && $(BIN)/uvicorn app.main:create_app --factory --reload --no-access-log --port 8000

# --- Docker ---------------------------------------------------------------------
.env:
	@echo "No .env found. Run: cp .env.example .env  (then change the passwords)" && exit 1

up: .env ## Build and start the full local stack, waiting for health checks
	docker compose up -d --build --wait

down: ## Stop the stack (keeps data volumes)
	docker compose down

down-volumes: ## Stop the stack AND delete local Postgres/Neo4j data
	docker compose down -v

logs: ## Follow logs of all services
	docker compose logs -f

ps: ## Show service status
	docker compose ps

smoke: .env ## Verify every running service actually answers
	./scripts/smoke_local_stack.sh

docker-build: ## Build the API image on its own
	docker build -t contextledger-api:local $(BACKEND)

# --- Benchmarks -----------------------------------------------------------------
metrics: ## Record foundation metrics (test count, image size) to benchmarks/results/
	$(BIN)/python benchmarks/scripts/collect_foundation_metrics.py

clean: ## Remove caches (not the virtualenv)
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -f $(BACKEND)/.coverage
