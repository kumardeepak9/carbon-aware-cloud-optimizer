"""Unit tests for the Phase 7 optimization safety validation layer."""

from __future__ import annotations

from agent.models import Action, ValidationStatus
from agent.policy import DecisionPolicy
from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
from agent.service import GreenOpsDecisionAgent
from monitoring.queries import GreenOpsQueries
from tests.unit.test_decision_policy import _observation


def test_safe_scale_down_recommendation_is_approved() -> None:
    recommendation = DecisionPolicy().recommend(_observation(), now=1_000.0)

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert validation.status is ValidationStatus.APPROVED
    assert validation.approved_for_gitops_change is True
    assert validation.safeguards_triggered == []


def test_safe_scale_up_recommendation_is_approved_for_reliability() -> None:
    recommendation = DecisionPolicy().recommend(
        _observation(cpu_request_ratio=0.90),
        now=1_000.0,
    )

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert recommendation.action is Action.SCALE_UP
    assert validation.status is ValidationStatus.APPROVED
    assert validation.approved_for_gitops_change is True


def test_missing_data_recommendation_is_rejected() -> None:
    observation = _observation(carbon_data_available=0.0)
    recommendation = DecisionPolicy().recommend(observation, now=1_000.0)

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert recommendation.action is Action.DEFER
    assert validation.status is ValidationStatus.REJECTED
    assert "carbon data is unavailable" in validation.reason
    assert validation.approved_for_gitops_change is False


def test_stale_carbon_data_is_rejected() -> None:
    recommendation = DecisionPolicy().recommend(
        _observation(carbon_last_update_timestamp_seconds=0.0),
        now=1_000.0,
    )

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert validation.status is ValidationStatus.REJECTED
    assert "carbon data is stale" in validation.reason


def test_scale_down_below_minimum_replicas_is_rejected() -> None:
    recommendation = DecisionPolicy().recommend(
        _observation(replica_count_desired=2.0, replica_count_ready=2.0),
        now=1_000.0,
    ).model_copy(update={"recommended_replicas": 0})

    validation = OptimizationSafetyPolicy(
        OptimizationSafetyConfig(min_replicas=1)
    ).validate(recommendation, now=1_000.0)

    assert validation.status is ValidationStatus.REJECTED
    assert "below minimum" in validation.reason


def test_unsafe_cpu_blocks_scale_down() -> None:
    recommendation = DecisionPolicy().recommend(_observation(), now=1_000.0)
    recommendation = recommendation.model_copy(
        update={
            "operational_context": recommendation.operational_context.model_copy(
                update={"cpu_request_ratio": 0.75}
            )
        }
    )

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert validation.status is ValidationStatus.REJECTED
    assert "CPU utilization" in validation.reason


def test_large_scale_down_requires_review() -> None:
    recommendation = DecisionPolicy().recommend(
        _observation(replica_count_desired=8.0, replica_count_ready=8.0),
        now=1_000.0,
    ).model_copy(update={"recommended_replicas": 3})

    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)

    assert validation.status is ValidationStatus.REQUIRE_REVIEW
    assert "scale-down reduction exceeds" in validation.reason
    assert validation.approved_for_gitops_change is False


def test_cooldown_requires_review_before_another_optimization() -> None:
    recommendation = DecisionPolicy().recommend(_observation(), now=1_000.0)

    validation = OptimizationSafetyPolicy().validate(
        recommendation,
        now=1_000.0,
        last_optimization_timestamp_seconds=800.0,
    )

    assert validation.status is ValidationStatus.REQUIRE_REVIEW
    assert "cooldown period has not elapsed" in validation.reason


async def test_agent_service_always_returns_policy_validated_recommendation() -> None:
    class FakePrometheusClient:
        async def collect_agent_observation(
            self,
            queries: GreenOpsQueries,
            *,
            namespace: str,
            deployment: str,
        ):
            return _observation()

    validated = await GreenOpsDecisionAgent(FakePrometheusClient()).recommend(GreenOpsQueries())

    assert validated.recommendation.action is Action.SCALE_DOWN
    assert validated.validation.status is ValidationStatus.APPROVED
