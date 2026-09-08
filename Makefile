# =============================================================================
# GreenOps AI - Developer Shortcuts
# =============================================================================
.DEFAULT_GOAL := help
SHELL := /bin/bash
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PIP := $(PYTHON) -m pip

GREEN := \033[0;32m
YELLOW := \033[0;33m
RESET := \033[0m

.PHONY: help install install-dev lint format type-check test test-unit \
	test-integration coverage clean docker-up docker-down docker-build \
	agent health gitops report chat chat-agent

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*? ## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*? ## "}; {printf "$(GREEN)%-22s$(RESET) %s\n", $$1, $$2}'

install: ## Install production dependencies
	$(PIP) install -e .

install-dev: ## Install development dependencies
	$(PIP) install -e ".[dev]"
	pre-commit install

lint: ## Run ruff lint checks
	$(PYTHON) -m ruff check .

format: ## Format Python code with ruff
	$(PYTHON) -m ruff format .

type-check: ## Run mypy static type checks
	$(PYTHON) -m mypy . --exclude tests/

test: ## Run the full test suite
	$(PYTHON) -m pytest

test-unit: ## Run unit tests
	$(PYTHON) -m pytest tests/unit/ -v

test-integration: ## Run integration tests
	$(PYTHON) -m pytest tests/integration/ -v

coverage: ## Generate HTML coverage report
	$(PYTHON) -m pytest --cov=. --cov-report=html
	@echo "$(YELLOW)Coverage report: htmlcov/index.html$(RESET)"

docker-up: ## Start the local Docker Compose stack
	docker compose up -d --build

docker-down: ## Stop the local Docker Compose stack
	docker compose down

docker-build: ## Build Docker Compose images
	docker compose build

agent: ## Run one read-only GreenOps recommendation cycle
	$(PYTHON) -m agent.agent

health: ## Run read-only integration health checks
	$(PYTHON) -m agent.health_cli

gitops: ## Prepare a review-first GreenOps GitOps change
	$(PYTHON) -m gitops.cli

report: ## Generate the weekly GreenOps report
	$(PYTHON) -m reports.report

chat: ## Ask one GreenOps chat question with Q="..."
	$(PYTHON) -m chat.cli "$(Q)"

chat-agent: ## Start the interactive GreenOps chat helper, or pass ARGS="..."
	$(PYTHON) scripts/chat_agent.py $(ARGS)

clean: ## Remove local caches and generated build/test artifacts
	find . -path ./.venv -prune -o -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -path ./.venv -prune -o -type f -name "*.pyc" -delete
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage dist build *.egg-info reports/output
	@echo "$(GREEN)Clean.$(RESET)"
