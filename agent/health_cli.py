"""CLI entry point for GreenOps end-to-end health checks."""

from __future__ import annotations

import asyncio

from agent.health import GreenOpsHealthChecker
from config import bootstrap
from config.settings import GitOpsSettings, PrometheusSettings
from monitoring.client import PrometheusClient


async def run_once() -> None:
    """Run read-only checks for Prometheus, GitOps config, and safety boundaries."""
    gitops = GitOpsSettings()
    prometheus = PrometheusSettings()
    async with PrometheusClient(base_url=prometheus.api_url) as client:
        report = await GreenOpsHealthChecker(
            prometheus_client=client,
            gitops_settings=gitops,
        ).check()
    print(report.model_dump_json(indent=2))


def main() -> None:
    """Run a single health-check cycle."""
    bootstrap()
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
