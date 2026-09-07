"""
tests/unit/test_end_to_end_wiring.py

Regression tests for integration seams found during the end-to-end production
validation:

  * the recommendation policy is configured from AGENT_* settings, not only the
    safety-validation policy (they used to disagree)
  * the default carbon-data-age tolerance accommodates Electricity Maps' hourly
    `latest` granularity
  * the weekly report consumes the persisted decision-history store
"""

from __future__ import annotations

import time

import pytest

from agent.policy import DecisionPolicy, PolicyConfig
from agent.safety import OptimizationSafetyConfig
from agent.service import (
    GreenOpsDecisionAgent,
    policy_config_from_settings,
    safety_config_from_settings,
)
from chat.history import DecisionHistoryStore, DecisionRecord
from config.settings import AgentSettings
from reports.generator import WeeklyReportGenerator, event_from_decision_record

# ---------------------------------------------------------------------------
# Policy is configured from settings
# ---------------------------------------------------------------------------


def test_policy_config_reflects_agent_settings() -> None:
    agent = AgentSettings(
        min_replicas=2,
        max_replicas=6,
        cpu_safety_threshold=0.6,
        latency_sla_threshold_seconds=0.5,
        max_carbon_data_age_seconds=7200,
    )
    pc = policy_config_from_settings(agent)
    assert pc.min_replicas == 2
    assert pc.max_replicas == 6
    assert pc.high_cpu_ratio == 0.6
    assert pc.high_p99_latency_seconds == 0.5
    assert pc.max_carbon_data_age_seconds == 7200.0


def test_safety_and_policy_configs_agree_on_shared_bounds() -> None:
    agent = AgentSettings(min_replicas=3, max_replicas=8, max_carbon_data_age_seconds=3600)
    pc = policy_config_from_settings(agent)
    sc = safety_config_from_settings(agent)
    assert pc.min_replicas == sc.min_replicas == 3
    assert pc.max_replicas == sc.max_replicas == 8
    assert pc.max_carbon_data_age_seconds == sc.max_carbon_data_age_seconds == 3600.0


def test_agent_from_settings_wires_both_policies() -> None:
    agent = AgentSettings(max_replicas=4, max_carbon_data_age_seconds=7200)
    built = GreenOpsDecisionAgent.from_settings(client=object(), agent=agent)  # type: ignore[arg-type]
    assert built._policy.config.max_replicas == 4  # noqa: SLF001
    assert built._policy.config.max_carbon_data_age_seconds == 7200.0  # noqa: SLF001
    assert built._safety_policy.config.max_replicas == 4  # noqa: SLF001


# ---------------------------------------------------------------------------
# Carbon-data-age default accommodates Electricity Maps hourly granularity
# ---------------------------------------------------------------------------


def test_carbon_age_defaults_tolerate_hourly_grid_data() -> None:
    # EM `latest` for estimated zones is stamped at the hour boundary, so a
    # "fresh" value is routinely 30-70 min old. Defaults must exceed one hour.
    assert AgentSettings().max_carbon_data_age_seconds >= 3600
    assert PolicyConfig().max_carbon_data_age_seconds >= 3600
    assert OptimizationSafetyConfig().max_carbon_data_age_seconds >= 3600


def test_hour_old_carbon_data_is_not_stale_by_default() -> None:
    from monitoring.models import AgentObservation, MetricSnapshot

    now = 1_000_000.0
    fifty_min_ago = now - 3000
    snaps = [
        MetricSnapshot(name=n, query=n, value=v, labels={}, timestamp=now, unit="")
        for n, v in {
            "carbon_intensity_gco2_kwh": 420.0,
            "carbon_data_available": 1.0,
            "carbon_last_update_timestamp_seconds": fifty_min_ago,
            "replica_count_desired": 3.0,
            "replica_count_ready": 3.0,
            "cpu_request_ratio": 0.2,
            "memory_request_ratio": 0.3,
            "http_request_rate_rps": 1.0,
            "http_error_rate_rps": 0.0,
            "http_p99_latency_seconds": 0.1,
            "pod_restart_rate": 0.0,
            "node_cpu_utilization_ratio": 0.4,
        }.items()
    ]
    decision = DecisionPolicy().recommend(
        AgentObservation(snapshots=snaps, collected_at=now), now=now
    )
    assert "fresh_carbon_data" not in decision.metadata.missing_signals


# ---------------------------------------------------------------------------
# Weekly report consumes the decision-history store
# ---------------------------------------------------------------------------


@pytest.fixture
def history(tmp_path) -> DecisionHistoryStore:
    store = DecisionHistoryStore(tmp_path / "decision-history.jsonl")
    now = time.time()
    store.append(
        DecisionRecord(
            lifecycle_id="lc-a",
            started_at=now - 3 * 86400,
            completed_at=now - 3 * 86400 + 200,
            action="SCALE_DOWN",
            reason="high carbon, low load",
            decision_basis="low_load_high_carbon",
            confidence=0.95,
            current_replicas=3,
            recommended_replicas=2,
            policy_status="APPROVED",
            approved_for_gitops_change=True,
            carbon_intensity_gco2_kwh=420.0,
            pre_snapshot={"replica_count_desired": 3, "http_p99_latency_seconds": 0.10},
            post_snapshot={"replica_count_desired": 2, "http_p99_latency_seconds": 0.11},
            gitops_status="PR_CREATED",
            verification_outcome="SUCCESS",
            final_outcome="SUCCESS",
        )
    )
    store.append(
        DecisionRecord(
            lifecycle_id="lc-b",
            started_at=now - 1 * 86400,
            action="SCALE_DOWN",
            reason="too aggressive",
            current_replicas=2,
            recommended_replicas=1,
            policy_status="REJECTED",
            policy_reason="exceeds max_scale_down_percentage",
            final_outcome="BLOCKED:REJECTED",
        )
    )
    return store


def test_event_from_decision_record_maps_core_fields(history: DecisionHistoryStore) -> None:
    rec = history.get("lc-a")
    assert rec is not None
    ev = event_from_decision_record(rec)
    assert ev.lifecycle_id == "lc-a"
    assert ev.action == "SCALE_DOWN"
    assert ev.policy_status == "APPROVED"
    assert ev.pre_replicas == 3 and ev.post_replicas == 2
    assert ev.replica_delta == -1
    assert ev.pre_p99_latency == 0.10 and ev.post_p99_latency == 0.11
    assert ev.was_applied and ev.final_outcome == "SUCCESS"


def test_weekly_report_counts_decision_history(history: DecisionHistoryStore) -> None:
    from datetime import UTC, datetime, timedelta

    records, _ = history.load()
    events = [event_from_decision_record(r) for r in records]
    end = datetime.now(UTC)
    report = WeeklyReportGenerator(events=events).generate(
        period_start=end - timedelta(days=7), period_end=end
    )
    assert report.total_optimization_cycles == 2
    assert report.total_approved == 1
    assert report.total_rejected == 1
    assert report.total_applied == 1
    assert {e.lifecycle_id for e in report.optimization_events} == {"lc-a", "lc-b"}


@pytest.mark.asyncio
async def test_report_metric_summaries_degrade_when_prometheus_down() -> None:
    import respx
    from httpx import ConnectError

    from reports.report import _load_metric_summaries

    with respx.mock:
        respx.get("http://prom-x:9090/-/healthy").mock(side_effect=ConnectError("down"))
        start = time.time() - 7 * 86400
        carbon, workload = await _load_metric_summaries(
            "http://prom-x:9090",
            "greenops",
            "greenops-demo-workload",
            _dt(start),
            _dt(time.time()),
        )
    assert carbon == {} and workload == {}


@pytest.mark.asyncio
async def test_report_metric_summaries_aggregate_live_shape() -> None:
    import respx
    from httpx import Response

    from reports.report import _load_metric_summaries

    ts = time.time() - 3600
    matrix = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[ts, "400"], [ts + 900, "440"]]}],
        },
    }
    with respx.mock:
        respx.get("http://prom-y:9090/-/healthy").mock(return_value=Response(200))
        respx.get("http://prom-y:9090/api/v1/query_range").mock(
            return_value=Response(200, json=matrix)
        )
        carbon, workload = await _load_metric_summaries(
            "http://prom-y:9090",
            "greenops",
            "greenops-demo-workload",
            _dt(time.time() - 7 * 86400),
            _dt(time.time()),
        )
    assert carbon["avg_intensity"] == 420.0  # mean of 400 and 440
    assert carbon["min_intensity"] == 400.0 and carbon["max_intensity"] == 440.0
    assert carbon["data_availability_pct"] == 42000.0  # 420 * 100 (same mocked series)


def _dt(epoch: float):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC)
