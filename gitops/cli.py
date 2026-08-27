"""CLI entry point for review-first GreenOps GitOps change preparation."""

from __future__ import annotations

import asyncio

from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
from agent.service import GreenOpsDecisionAgent
from config.settings import AgentSettings, GitOpsSettings, KubernetesSettings, PrometheusSettings
from gitops.workflow import GitOpsChangeWorkflow
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


async def run_once() -> None:
    """Collect a validated recommendation and prepare a review-first GitOps change."""
    agent = AgentSettings()
    gitops = GitOpsSettings()
    kubernetes = KubernetesSettings()
    prometheus = PrometheusSettings()
    queries = GreenOpsQueries(
        namespace=kubernetes.namespace,
        deployment=gitops.deployment_name,
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
    result = await GitOpsChangeWorkflow(gitops).prepare_change(validated)
    print(result.model_dump_json(indent=2))


def main() -> None:
    """Run one review-first GitOps preparation cycle."""
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
