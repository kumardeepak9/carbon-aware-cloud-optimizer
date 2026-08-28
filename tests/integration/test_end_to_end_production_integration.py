"""End-to-end integration tests for the GreenOps autonomous workflow."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.controller import ClosedLoopController
from agent.health import GreenOpsHealthChecker, HealthStatus
from agent.models import ValidationStatus
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

    async def prepare_change(self, validated):
        self.validated_statuses.append(validated.validation.status)
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
        period_start=datetime.fromtimestamp(0, tz=timezone.utc),
        period_end=datetime.fromtimestamp(3600, tz=timezone.utc),
    )

    assert gitops.validated_statuses == [ValidationStatus.APPROVED]
    assert lifecycle.gitops_status is GitOpsChangeStatus.PREPARED
    assert lifecycle.verification_outcome is VerificationOutcome.SUCCESS
    assert lifecycle.final_outcome == "SUCCESS"
    assert report.total_optimization_cycles == 1
    assert report.optimization_events[0].policy_status == "APPROVED"


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
