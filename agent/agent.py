"""CLI entry point for a single read-only GreenOps recommendation cycle."""

from __future__ import annotations

import asyncio

from agent.service import GreenOpsDecisionAgent
from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
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
    safety_config = OptimizationSafetyConfig(
        min_replicas=agent.min_replicas,
        max_replicas=agent.max_replicas,
        cpu_safety_threshold=agent.cpu_safety_threshold,
        latency_sla_threshold_seconds=agent.latency_sla_threshold_seconds,
        max_scale_down_percentage=agent.max_scale_down_percentage,
        cooldown_seconds=agent.optimization_cooldown_seconds,
        max_carbon_data_age_seconds=agent.max_carbon_data_age_seconds,
    )
    async with PrometheusClient(base_url=prometheus.api_url) as client:
        validated = await GreenOpsDecisionAgent(
            client,
            safety_policy=OptimizationSafetyPolicy(safety_config),
        ).recommend(queries)
    print(validated.model_dump_json(indent=2))


def main() -> None:
    """Run one recommendation cycle for manual or scheduled read-only use."""
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
