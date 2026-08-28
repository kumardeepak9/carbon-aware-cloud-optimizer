# =============================================================================
# Carbon-Aware Cloud Optimizer — Developer Shortcuts
# =============================================================================
.DEFAULT_GOAL := help
SHELL         := /bin/bash
PYTHON        := python3
PIP           := $(PYTHON) -m pip

# Colour helpers
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RESET  := \033[0m

.PHONY: help install install-dev lint format type-check test test-unit \
        test-integration coverage clean docker-up docker-down \
        docker-build agent health gitops report

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-22s$(RESET) %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
install:  ## Install production dependencies
	$(PIP) install -e .

install-dev:  ## Install all dependencies including dev tools
	$(PIP) install -e ".[dev]"
	pre-commit install

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------
lint:  ## Run ruff linter
	ruff check .

format:  ## Auto-format with ruff
	ruff format .

type-check:  ## Run mypy static type checker
	mypy . --exclude tests/

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
test:  ## Run full test suite
	pytest

test-unit:  ## Run unit tests only
	pytest tests/unit/ -v

test-integration:  ## Run integration tests only
	pytest tests/integration/ -v

coverage:  ## Generate HTML coverage report
	pytest --cov=. --cov-report=html
	@echo "$(YELLOW)Coverage report: htmlcov/index.html$(RESET)"

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-up:  ## Start local dev stack (Prometheus + Grafana + agent)
	docker compose up -d

docker-down:  ## Stop local dev stack
	docker compose down

docker-build:  ## Build all Docker images
	docker compose build

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
agent:  ## Run the GreenOps AI agent locally
	$(PYTHON) -m agent.agent

health:  ## Run read-only GreenOps integration health checks
	$(PYTHON) -m agent.health_cli

gitops:  ## Prepare a review-first GreenOps GitOps change
	$(PYTHON) -m gitops.cli

report:  ## Generate a GreenOps weekly report
	$(PYTHON) -m reporting.report

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
clean:  ## Remove build artifacts and caches
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage dist build *.egg-info
	@echo "$(GREEN)Clean.$(RESET)"
