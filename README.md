# Carbon-Aware Cloud Optimizer

> **GreenOps AI** — An intelligent, carbon-aware workload scheduler for Kubernetes clusters.

[![CI](https://github.com/your-org/carbon-aware-cloud-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/carbon-aware-cloud-optimizer/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org)

---

## Overview

The Carbon-Aware Cloud Optimizer continuously monitors the **carbon intensity of the electricity grid** (via [Electricity Maps](https://electricitymaps.com)) and uses an **AI agent** to make real-time scheduling decisions — deferring, scaling, or shifting Kubernetes workloads to minimise carbon emissions without sacrificing reliability.

```
Electricity Maps ──► AI Agent ──► Policy Validation ──► GitHub GitOps ──► Argo CD ──► K8s Cluster
                         │
                         └──► Prometheus ──► Grafana
                                   │
                                   └──► Weekly GreenOps Report ──► User
```

---

## Architecture

| Component | Responsibility |
|---|---|
| `carbon/` | Ingest live grid & carbon-intensity data from Electricity Maps |
| `agent/` | AI decision loop plus deterministic safety validation |
| `monitoring/` | Prometheus metrics + Grafana dashboards |
| `reporting/` | Weekly GreenOps PDF/HTML report generation |
| `k8s/` | Kubernetes manifests (Kustomize base + overlays) |
| `gitops/` | Review-first GitHub GitOps branch, commit, and PR preparation |
| `config/` | Environment-variable-driven settings + structured logging |
| `tests/` | Unit and integration test suite |
| `docs/` | Architecture, runbooks, ADRs |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- `kubectl` + a running cluster (for Kubernetes features)

### Local Development

```bash
# 1. Clone and enter
git clone https://github.com/your-org/carbon-aware-cloud-optimizer.git
cd carbon-aware-cloud-optimizer

# 2. Install dependencies
make install-dev

# 3. Configure environment
cp .env.example .env
# Edit .env — set ELECTRICITY_MAPS_API_KEY at minimum

# 4. Start the local stack (Prometheus + Grafana + agent)
make docker-up

# 5. Run agent locally (without Docker)
make agent

# Validate integration readiness
make health

# Prepare a review-first GitOps change from an approved decision
make gitops
```

### Running Tests

```bash
make test          # full suite
make test-unit     # unit tests only
make coverage      # HTML coverage report
```

---

## Project Layout

```
carbon-aware-cloud-optimizer/
├── carbon/          # Grid & carbon data ingestion
├── agent/           # AI agent & decision logic
├── monitoring/      # Prometheus metrics + Grafana dashboards
├── reporting/       # Weekly GreenOps report generation
├── k8s/             # Kubernetes manifests (Kustomize)
├── gitops/          # GitOps + Argo CD configuration
├── config/          # Settings & logging
├── tests/           # Unit + integration tests
├── docs/            # Architecture, runbooks, ADRs
├── docker/          # Dockerfiles
├── scripts/         # Helper scripts
├── .github/         # CI/CD workflows
├── docker-compose.yml
├── pyproject.toml
└── Makefile
```

---

## Configuration

All configuration is driven by environment variables.  
Copy `.env.example` → `.env` and populate before running.

> ⚠️ **Never commit `.env` or any file containing real API keys or passwords.**

---

## Contributing

See [docs/onboarding.md](docs/onboarding.md) for the development workflow.

## Production Integration

The end-to-end GreenOps AI workflow is documented in
[docs/e2e-production-integration.md](docs/e2e-production-integration.md).
It covers the Electricity Maps → Prometheus → AI Agent → Safety Policy →
GitHub GitOps → Argo CD → Kubernetes → Prometheus verification loop and the
weekly reporting path.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
