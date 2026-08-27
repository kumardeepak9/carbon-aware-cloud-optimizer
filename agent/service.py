"""Read-only orchestration of Prometheus collection and deterministic policy."""

from __future__ import annotations

from agent.models import DecisionRecommendation
from agent.policy import DecisionPolicy
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


class GreenOpsDecisionAgent:
    """Collects observations and returns recommendations; it never applies them."""

    def __init__(self, client: PrometheusClient, policy: DecisionPolicy | None = None) -> None:
        self._client = client
        self._policy = policy or DecisionPolicy()

    async def recommend(self, queries: GreenOpsQueries) -> DecisionRecommendation:
        observation = await self._client.collect_agent_observation(
            queries, namespace=queries.namespace, deployment=queries.deployment
        )
        return self._policy.recommend(observation)
