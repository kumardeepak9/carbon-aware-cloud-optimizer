"""Safety-critical unit tests for the Phase 6 read-only decision policy."""

from __future__ import annotations

import time

from agent.models import Action
from agent.policy import DecisionPolicy
from monitoring.models import AgentObservation, MetricSnapshot


def _observation(**overrides: float) -> AgentObservation:
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
            name=name, query=name, value=value, timestamp=time.time(), unit="",
            labels={"zone": "DE"} if name.startswith("carbon_") or name.endswith("percentage") else {},
        )
        for name, value in values.items()
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


def test_unhealthy_application_recommends_scale_up_even_when_carbon_is_high() -> None:
    decision = DecisionPolicy().recommend(_observation(replica_count_ready=2.0, pod_availability_ratio=2 / 3))

    assert decision.action is Action.SCALE_UP
    assert "not_all_replicas_ready" in decision.metadata.safety_guards_triggered
    assert decision.metadata.decision_basis == "reliability_priority"


def test_missing_data_defers_without_a_replica_target() -> None:
    observation = _observation()
    observation.snapshots = [
        snapshot for snapshot in observation.snapshots if snapshot.name != "carbon_intensity_gco2_kwh"
    ]

    decision = DecisionPolicy().recommend(observation)

    assert decision.action is Action.DEFER
    assert decision.recommended_replicas is None
    assert "carbon_intensity_gco2_kwh" in decision.metadata.missing_signals
    assert decision.metadata.confidence == 0.0
