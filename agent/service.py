"""Read-only orchestration of recommendation and mandatory safety validation."""

from __future__ import annotations

from agent.models import ValidatedRecommendation
from agent.policy import DecisionPolicy, PolicyConfig
from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
from config.settings import AgentSettings
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


def policy_config_from_settings(agent: AgentSettings) -> PolicyConfig:
    """Build the recommendation-policy config from ``AGENT_*`` settings.

    The operational bounds and SLA thresholds an operator configures must reach
    the recommendation policy, not only the safety-validation policy — otherwise
    the two layers disagree (e.g. the policy proposes 10 replicas while the
    operator set ``AGENT_MAX_REPLICAS=6``, or the policy calls hour-old grid
    data "stale" while ``AGENT_MAX_CARBON_DATA_AGE_SECONDS`` says it is fresh).
    Pure decision thresholds with no env var keep their reviewed code defaults.
    """
    return PolicyConfig(
        min_replicas=agent.min_replicas,
        max_replicas=agent.max_replicas,
        high_cpu_ratio=agent.cpu_safety_threshold,
        high_p99_latency_seconds=agent.latency_sla_threshold_seconds,
        max_carbon_data_age_seconds=float(agent.max_carbon_data_age_seconds),
    )


def safety_config_from_settings(agent: AgentSettings) -> OptimizationSafetyConfig:
    """Build the safety-validation config from ``AGENT_*`` settings."""
    return OptimizationSafetyConfig(
        min_replicas=agent.min_replicas,
        max_replicas=agent.max_replicas,
        cpu_safety_threshold=agent.cpu_safety_threshold,
        latency_sla_threshold_seconds=agent.latency_sla_threshold_seconds,
        max_scale_down_percentage=agent.max_scale_down_percentage,
        cooldown_seconds=float(agent.optimization_cooldown_seconds),
        max_carbon_data_age_seconds=float(agent.max_carbon_data_age_seconds),
    )


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

    @classmethod
    def from_settings(cls, client: PrometheusClient, agent: AgentSettings) -> GreenOpsDecisionAgent:
        """Construct with both policies configured from ``AGENT_*`` settings."""
        return cls(
            client,
            policy=DecisionPolicy(policy_config_from_settings(agent)),
            safety_policy=OptimizationSafetyPolicy(safety_config_from_settings(agent)),
        )

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
