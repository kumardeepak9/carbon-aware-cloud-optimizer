"""CLI entry point for a single read-only GreenOps recommendation cycle."""

from __future__ import annotations

import asyncio

from agent.service import GreenOpsDecisionAgent
from config import bootstrap
from config.settings import AgentSettings, KubernetesSettings, PrometheusSettings
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


async def run_once() -> None:
    """Read Prometheus state and print validated JSON; never apply it."""
    agent = AgentSettings()
    kubernetes = KubernetesSettings()
    prometheus = PrometheusSettings()
    queries = GreenOpsQueries(
        namespace=kubernetes.namespace,
        deployment="greenops-demo-workload",
    )
    async with PrometheusClient(base_url=prometheus.api_url) as client:
        validated = await GreenOpsDecisionAgent.from_settings(client, agent).recommend(queries)
    print(validated.model_dump_json(indent=2))


def main() -> None:
    """Run one recommendation cycle for manual or scheduled read-only use."""
    bootstrap()
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
