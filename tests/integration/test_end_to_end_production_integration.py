"""End-to-end integration tests for the GreenOps autonomous workflow."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.controller import ClosedLoopController
from agent.health import GreenOpsHealthChecker, HealthStatus
from agent.models import Action, ValidationStatus
from agent.service import GreenOpsDecisionAgent
from agent.verification import VerificationConfig, VerificationOutcome
from gitops.models import GitOpsChangeResult, GitOpsChangeStatus, GitOpsSettings
from monitoring.models import AgentObservation, MetricSnapshot
from monitoring.queries import GreenOpsQueries
from reports.generator import WeeklyReportGenerator


def _observation(values: dict[str, float]) -> AgentObservation:
    return AgentObservation(
        snapshots=[
            MetricSnapshot(
                name=name,
                query=name,
                value=value,
                labels={"zone": "DE"} if name.startswith("carbon_") else {},
                timestamp=time.time(),
                unit="",
            )
            for name, value in values.items()
        ],
        collected_at=time.time(),
        namespace="greenops",
        deployment="greenops-demo-workload",
    )


def _full_metric_state(*, replicas: float) -> dict[str, float]:
    now = time.time()
    return {
        "carbon_intensity_gco2_kwh": 325.0,
        "renewable_percentage": 22.0,
        "fossil_fuel_percentage": 61.0,
        "low_carbon_percentage": 39.0,
        "carbon_data_available": 1.0,
        "carbon_last_update_timestamp_seconds": now,
        "replica_count_desired": replicas,
        "replica_count_ready": replicas,
        "pod_availability_ratio": 1.0,
        "pod_restart_rate": 0.0,
        "cpu_request_ratio": 0.25 if replicas >= 3 else 0.52,
        "memory_request_ratio": 0.30 if replicas >= 3 else 0.48,
        "http_request_rate_rps": 1.0,
        "http_error_rate_rps": 0.0,
        "http_p99_latency_seconds": 0.15 if replicas >= 3 else 0.22,
        "http_p50_latency_seconds": 0.05,
        "node_cpu_utilization_ratio": 0.50,
        "node_memory_available_bytes": 1_000_000.0,
    }


class FakePrometheusClient:
    def __init__(self) -> None:
        self.calls = 0

    async def is_healthy(self) -> bool:
        return True

    async def collect_agent_observation(
        self,
        queries: GreenOpsQueries,
        namespace: str = "greenops",
        deployment: str = "greenops-demo-workload",
    ) -> AgentObservation:
        self.calls += 1
        replicas = 3.0 if self.calls <= 2 else 2.0
        return _observation(_full_metric_state(replicas=replicas))


class FakeGitOpsWorkflow:
    def __init__(self) -> None:
        self.validated_statuses: list[ValidationStatus] = []
        self.recommendations = []

    async def prepare_change(self, validated):
        self.validated_statuses.append(validated.validation.status)
        self.recommendations.append(validated.recommendation)
        return GitOpsChangeResult(
            status=GitOpsChangeStatus.PREPARED,
            reason="GitOps branch and commit prepared for review.",
            branch_name="greenops/scale-down-greenops-demo-workload-to-2-low-load-high-carbon",
            commit_sha="abc123",
            pull_request_title="GreenOps: Scale Down greenops-demo-workload to 2 replicas",
            pull_request_body="Policy validation: APPROVED",
            changed_files=["k8s/overlays/prod/kustomization.yaml"],
            manifest_path="k8s/overlays/prod/kustomization.yaml",
        )


class SequencedPrometheusClient:
    def __init__(self, observations: list[dict[str, float]]) -> None:
        self._observations = observations
        self.calls = 0

    async def is_healthy(self) -> bool:
        return True

    async def collect_agent_observation(
        self,
        queries: GreenOpsQueries,
        namespace: str = "greenops",
        deployment: str = "greenops-demo-workload",
    ) -> AgentObservation:
        index = min(self.calls, len(self._observations) - 1)
        self.calls += 1
        return _observation(self._observations[index])


class RecordingGitOpsWorkflow:
    def __init__(self) -> None:
        self.recommendations = []

    async def prepare_change(self, validated):
        self.recommendations.append(validated.recommendation)
        if len(self.recommendations) == 1:
            return GitOpsChangeResult(
                status=GitOpsChangeStatus.PR_CREATED,
                reason="Optimization PR prepared for review.",
                branch_name="greenops/scale-down-greenops-demo-workload-to-2-low-load-high-carbon",
                commit_sha="scale-down-sha",
                pull_request_url="https://github.example/pulls/1",
                changed_files=["k8s/overlays/prod/kustomization.yaml"],
                manifest_path="k8s/overlays/prod/kustomization.yaml",
            )
        return GitOpsChangeResult(
            status=GitOpsChangeStatus.PR_CREATED,
            reason="Rollback PR prepared for review.",
            branch_name="greenops/scale-up-greenops-demo-workload-to-3-emergency-rollback",
            commit_sha="rollback-sha",
            pull_request_url="https://github.example/pulls/2",
            changed_files=["k8s/overlays/prod/kustomization.yaml"],
            manifest_path="k8s/overlays/prod/kustomization.yaml",
        )


@pytest.mark.asyncio
async def test_full_architecture_flow_produces_verified_report_event() -> None:
    prometheus = FakePrometheusClient()
    gitops = FakeGitOpsWorkflow()
    agent = GreenOpsDecisionAgent(prometheus)
    controller = ClosedLoopController(
        prometheus_client=prometheus,
        queries=GreenOpsQueries(),
        decision_agent=agent,
        gitops_workflow=gitops,
        verification_config=VerificationConfig(
            stabilization_period_seconds=0.0,
            min_deployment_wait_seconds=0.0,
            min_required_metrics=4,
        ),
    )

    lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)
    report = WeeklyReportGenerator(
        lifecycles=[lifecycle],
        carbon_summary={
            "avg_intensity": 325.0,
            "avg_renewable_pct": 22.0,
            "avg_fossil_pct": 61.0,
            "data_availability_pct": 100.0,
        },
        workload_summary={
            "avg_cpu_ratio": 0.38,
            "avg_memory_ratio": 0.39,
            "avg_replicas": 2.5,
            "avg_request_rate": 1.0,
            "avg_p99_latency": 0.19,
            "avg_availability": 1.0,
        },
        region="DE",
    ).generate(
        period_start=datetime.fromtimestamp(0, tz=UTC),
        period_end=datetime.fromtimestamp(3600, tz=UTC),
    )

    assert gitops.validated_statuses == [ValidationStatus.APPROVED]
    assert lifecycle.gitops_status is GitOpsChangeStatus.PREPARED
    assert lifecycle.verification_outcome is VerificationOutcome.SUCCESS
    assert lifecycle.final_outcome == "SUCCESS"
    assert report.total_optimization_cycles == 1
    assert report.optimization_events[0].policy_status == "APPROVED"


@pytest.mark.asyncio
async def test_complete_feedback_loop_successful_scale_down() -> None:
    before = _full_metric_state(replicas=3.0)
    after = _full_metric_state(replicas=2.0)
    prometheus = SequencedPrometheusClient([before, before, before, after])
    gitops = RecordingGitOpsWorkflow()

    controller = ClosedLoopController(
        prometheus_client=prometheus,
        queries=GreenOpsQueries(),
        decision_agent=GreenOpsDecisionAgent(prometheus),
        gitops_workflow=gitops,
        verification_config=VerificationConfig(
            stabilization_period_seconds=0.0,
            min_deployment_wait_seconds=0.0,
        ),
    )

    lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)

    assert prometheus.calls == 4
    assert len(gitops.recommendations) == 1
    assert gitops.recommendations[0].action is Action.SCALE_DOWN
    assert gitops.recommendations[0].recommended_replicas == 2
    assert lifecycle.gitops_status is GitOpsChangeStatus.PR_CREATED
    assert lifecycle.verification_outcome is VerificationOutcome.SUCCESS
    assert lifecycle.final_outcome == "SUCCESS"
    assert lifecycle.post_snapshot_json["replica_count_desired"] == 2.0
    assert lifecycle.post_snapshot_json["cpu_request_ratio"] == after["cpu_request_ratio"]
    assert lifecycle.post_snapshot_json["memory_request_ratio"] == after["memory_request_ratio"]
    assert lifecycle.post_snapshot_json["http_request_rate_rps"] == after["http_request_rate_rps"]
    assert lifecycle.post_snapshot_json["http_p99_latency_seconds"] == after["http_p99_latency_seconds"]
    assert lifecycle.post_snapshot_json["availability_ratio"] == after["pod_availability_ratio"]
    assert lifecycle.metric_deltas_json["replica_count_desired"]["after"] == 2.0
    assert lifecycle.rollback_prepared is False


@pytest.mark.asyncio
async def test_complete_feedback_loop_harmful_scale_down_prepares_gitops_rollback() -> None:
    before = _full_metric_state(replicas=3.0)
    harmful_after = {
        **_full_metric_state(replicas=2.0),
        "cpu_request_ratio": 0.96,
        "memory_request_ratio": 0.91,
        "http_request_rate_rps": 1.2,
        "http_error_rate_rps": 0.05,
        "http_p99_latency_seconds": 1.7,
        "pod_availability_ratio": 1.0,
        "pod_restart_rate": 0.0,
    }
    prometheus = SequencedPrometheusClient([before, before, before, harmful_after])
    gitops = RecordingGitOpsWorkflow()

    controller = ClosedLoopController(
        prometheus_client=prometheus,
        queries=GreenOpsQueries(),
        decision_agent=GreenOpsDecisionAgent(prometheus),
        gitops_workflow=gitops,
        verification_config=VerificationConfig(
            stabilization_period_seconds=0.0,
            min_deployment_wait_seconds=0.0,
        ),
    )

    lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)

    assert len(gitops.recommendations) == 2
    assert gitops.recommendations[0].action is Action.SCALE_DOWN
    rollback = gitops.recommendations[1]
    assert rollback.action is Action.SCALE_UP
    assert rollback.current_replicas == 2
    assert rollback.recommended_replicas == 3
    assert rollback.metadata.decision_basis == "emergency-rollback"
    assert rollback.operational_context.memory_request_ratio == harmful_after["memory_request_ratio"]
    assert rollback.operational_context.request_rate_rps == harmful_after["http_request_rate_rps"]
    assert lifecycle.verification_outcome is VerificationOutcome.ROLLBACK_REQUIRED
    assert lifecycle.final_outcome == "ROLLBACK_PREPARED"
    assert lifecycle.rollback_prepared is True
    assert lifecycle.rollback_branch == "greenops/scale-up-greenops-demo-workload-to-3-emergency-rollback"
    assert lifecycle.rollback_commit_sha == "rollback-sha"
    assert lifecycle.safety_thresholds_violated


@pytest.mark.asyncio
async def test_health_checker_reports_ready_components(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest = repo / "k8s" / "overlays" / "prod" / "kustomization.yaml"
    (repo / ".git").mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    manifest.write_text("apiVersion: kustomize.config.k8s.io/v1beta1\n", encoding="utf-8")

    report = await GreenOpsHealthChecker(
        prometheus_client=FakePrometheusClient(),
        gitops_settings=GitOpsSettings(repo_path=repo),
    ).check()

    assert report.status is HealthStatus.HEALTHY
    assert {component.component for component in report.components} == {
        "prometheus",
        "gitops",
        "safety_policy",
    }


def test_no_component_bypasses_gitops_or_policy_boundaries() -> None:
    source_roots = [Path("agent"), Path("gitops")]
    forbidden = ("kubernetes.client", "kubectl", "argocd app sync")
    offenders: list[str] = []
    for root in source_roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.as_posix() == "agent/health.py":
                continue
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path}:{token}")

    assert offenders == []
