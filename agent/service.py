"""Read-only orchestration of recommendation and mandatory safety validation."""

from __future__ import annotations

from agent.models import ValidatedRecommendation
from agent.policy import DecisionPolicy
from agent.safety import OptimizationSafetyPolicy
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


class GreenOpsDecisionAgent:
    """Collects observations and returns only policy-validated recommendations."""

    def __init__(
        self,
        client: PrometheusClient,
        policy: DecisionPolicy | None = None,
        safety_policy: OptimizationSafetyPolicy | None = None,
    ) -> None:
        self._client = client
        self._policy = policy or DecisionPolicy()
        self._safety_policy = safety_policy or OptimizationSafetyPolicy()

    async def recommend(
        self,
        queries: GreenOpsQueries,
        *,
        last_optimization_timestamp_seconds: float | None = None,
    ) -> ValidatedRecommendation:
        observation = await self._client.collect_agent_observation(
            queries, namespace=queries.namespace, deployment=queries.deployment
        )
        recommendation = self._policy.recommend(observation)
        validation = self._safety_policy.validate(
            recommendation,
            last_optimization_timestamp_seconds=last_optimization_timestamp_seconds,
        )
        return ValidatedRecommendation(recommendation=recommendation, validation=validation)
