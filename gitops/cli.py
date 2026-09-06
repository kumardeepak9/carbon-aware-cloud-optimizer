"""CLI entry point for review-first GreenOps GitOps change preparation."""

from __future__ import annotations

import asyncio

from agent.service import GreenOpsDecisionAgent
from config import bootstrap
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
    async with PrometheusClient(base_url=prometheus.api_url) as client:
        validated = await GreenOpsDecisionAgent.from_settings(client, agent).recommend(queries)
    result = await GitOpsChangeWorkflow(gitops).prepare_change(validated)
    print(result.model_dump_json(indent=2))


def main() -> None:
    """Run one review-first GitOps preparation cycle."""
    bootstrap()
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
