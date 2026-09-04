"""Safety-critical unit tests for the Phase 6 read-only decision policy."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from agent.models import Action, DecisionRecommendation
from agent.policy import DecisionPolicy
from monitoring.models import AgentObservation, MetricSnapshot


def _observation(**overrides: float | None) -> AgentObservation:
    values = {
        "carbon_intensity_gco2_kwh": 320.0,
        "carbon_data_available": 1.0,
        "carbon_last_update_timestamp_seconds": time.time(),
        "replica_count_desired": 3.0,
        "replica_count_ready": 3.0,
        "pod_availability_ratio": 1.0,
        "cpu_request_ratio": 0.25,
        "memory_request_ratio": 0.30,
        "http_request_rate_rps": 1.0,
        "http_error_rate_rps": 0.0,
        "http_p99_latency_seconds": 0.15,
        "pod_restart_rate": 0.0,
        "node_cpu_utilization_ratio": 0.50,
        "node_memory_available_bytes": 1_000_000.0,
        "renewable_percentage": 20.0,
        "fossil_fuel_percentage": 65.0,
        "low_carbon_percentage": 35.0,
    }
    values.update(overrides)
    snapshots = [
        MetricSnapshot(
            name=name,
            query=name,
            value=value,
            timestamp=time.time(),
            unit="",
            labels={"zone": "DE"}
            if name.startswith("carbon_") or name.endswith("percentage")
            else {},
        )
        for name, value in values.items()
        if value is not None
    ]
    return AgentObservation(snapshots=snapshots, collected_at=time.time())


def test_low_load_high_carbon_recommends_scale_down() -> None:
    decision = DecisionPolicy().recommend(_observation())

    assert decision.action is Action.SCALE_DOWN
    assert decision.current_replicas == 3
    assert decision.recommended_replicas == 2
    assert decision.environmental_context.region == "DE"
    assert decision.metadata.read_only is True


def test_high_load_high_carbon_recommends_scale_up_for_reliability() -> None:
    decision = DecisionPolicy().recommend(_observation(cpu_request_ratio=0.90))

    assert decision.action is Action.SCALE_UP
    assert decision.recommended_replicas == 4
    assert decision.metadata.decision_basis == "high_operational_load"


def test_low_load_low_carbon_recommends_keep() -> None:
    decision = DecisionPolicy().recommend(_observation(carbon_intensity_gco2_kwh=80.0))

    assert decision.action is Action.KEEP
    assert decision.current_replicas == 3
    assert decision.recommended_replicas == 3
    assert decision.metadata.decision_basis == "steady_state"


def test_unhealthy_application_recommends_scale_up_even_when_carbon_is_high() -> None:
    decision = DecisionPolicy().recommend(
        _observation(replica_count_ready=2.0, pod_availability_ratio=2 / 3)
    )

    assert decision.action is Action.SCALE_UP
    assert "not_all_replicas_ready" in decision.metadata.safety_guards_triggered
    assert decision.metadata.decision_basis == "reliability_priority"


def test_missing_carbon_data_defers_without_a_replica_target() -> None:
    observation = _observation()
    observation.snapshots = [
        snapshot
        for snapshot in observation.snapshots
        if snapshot.name != "carbon_intensity_gco2_kwh"
    ]

    decision = DecisionPolicy().recommend(observation)

    assert decision.action is Action.DEFER
    assert decision.recommended_replicas is None
    assert "carbon_intensity_gco2_kwh" in decision.metadata.missing_signals
    assert decision.metadata.confidence == 0.0


def test_missing_prometheus_data_defers_without_a_replica_target() -> None:
    decision = DecisionPolicy().recommend(_observation(cpu_request_ratio=None))

    assert decision.action is Action.DEFER
    assert decision.recommended_replicas is None
    assert "cpu_request_ratio" in decision.metadata.missing_signals


def test_stale_metrics_defer_without_a_replica_target() -> None:
    decision = DecisionPolicy().recommend(
        _observation(carbon_last_update_timestamp_seconds=100.0),
        now=1_000.0,
    )

    assert decision.action is Action.DEFER
    assert decision.recommended_replicas is None
    assert "fresh_carbon_data" in decision.metadata.missing_signals


def test_every_decision_contains_required_context() -> None:
    decision = DecisionPolicy().recommend(_observation(), now=1_000.0)
    dumped = decision.model_dump()

    assert dumped["action"] == Action.SCALE_DOWN
    assert dumped["current_replicas"] == 3
    assert dumped["recommended_replicas"] == 2
    assert dumped["reason"]
    assert dumped["environmental_context"]["carbon_intensity_gco2_kwh"] == 320.0
    assert dumped["operational_context"]["cpu_request_ratio"] == 0.25


@pytest.mark.parametrize("bad_action", ["DELETE_CLUSTER", "PATCH_DEPLOYMENT", ""])
def test_malformed_structured_output_rejects_unsupported_actions(bad_action: str) -> None:
    good = DecisionPolicy().recommend(_observation(), now=1_000.0).model_dump()
    good["action"] = bad_action

    with pytest.raises(ValidationError):
        DecisionRecommendation.model_validate(good)


def test_malformed_scale_output_without_target_is_rejected() -> None:
    good = DecisionPolicy().recommend(_observation(), now=1_000.0).model_dump()
    good["recommended_replicas"] = None

    with pytest.raises(ValidationError):
        DecisionRecommendation.model_validate(good)


def test_invalid_operational_metric_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DecisionPolicy().recommend(_observation(pod_availability_ratio=1.5), now=1_000.0)
