"""
tests/integration/test_closed_loop.py

Integration tests for the Phase 10 closed-loop optimization controller.

These tests exercise the full lifecycle path — from metric collection through
verification and rollback — using mock Prometheus data and a fake GitOps
workflow (no real Git, GitHub API, or Kubernetes required).

Test classes
------------
TestWorkloadSnapshot          : snapshot construction from observation map.
TestMetricDelta               : delta computation and edge cases.
TestVerificationConfig        : default thresholds are sensible.
TestOptimizationVerifierSuccess    : verifier classifies SUCCESS correctly.
TestOptimizationVerifierDegraded   : verifier classifies DEGRADED correctly.
TestOptimizationVerifierRollback   : verifier detects violations and prepares rollback.
TestOptimizationVerifierInconclusive: verifier handles missing/sparse data.
TestOptimizationLifecycle     : audit event sequencing and summary.
TestClosedLoopController      : full end-to-end lifecycle with mock dependencies.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import LifecycleStage, OptimizationLifecycle
from agent.models import (
    Action,
    DecisionMetadata,
    DecisionRecommendation,
    EnvironmentalContext,
    OperationalContext,
    PolicyValidation,
    ValidatedRecommendation,
    ValidationStatus,
)
from agent.verification import (
    MetricDelta,
    OptimizationVerifier,
    VerificationConfig,
    VerificationOutcome,
    WorkloadSnapshot,
    compute_deltas,
)
from gitops.models import GitOpsChangeResult, GitOpsChangeStatus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def healthy_pre_snapshot() -> WorkloadSnapshot:
    return WorkloadSnapshot(
        replica_count_desired=2.0,
        replica_count_ready=2.0,
        availability_ratio=1.0,
        cpu_request_ratio=0.40,
        memory_request_ratio=0.55,
        http_request_rate_rps=10.0,
        http_error_rate_rps=0.0,
        http_p99_latency_seconds=0.12,
        http_p50_latency_seconds=0.05,
        pod_restart_rate=0.0,
    )


@pytest.fixture()
def healthy_post_obs() -> dict[str, float]:
    """Healthy post-optimization observation map (scale-down to 1 replica)."""
    return {
        "replica_count_desired": 1.0,
        "replica_count_ready": 1.0,
        "pod_availability_ratio": 1.0,
        "cpu_request_ratio": 0.55,
        "memory_request_ratio": 0.60,
        "http_request_rate_rps": 9.5,
        "http_error_rate_rps": 0.0,
        "http_p99_latency_seconds": 0.18,
        "http_p50_latency_seconds": 0.07,
        "pod_restart_rate": 0.0,
    }


@pytest.fixture()
def scale_down_validated() -> ValidatedRecommendation:
    return ValidatedRecommendation(
        recommendation=DecisionRecommendation(
            action=Action.SCALE_DOWN,
            current_replicas=2,
            recommended_replicas=1,
            reason="High carbon intensity; workload can tolerate 1 replica.",
            environmental_context=EnvironmentalContext(
                carbon_intensity_gco2_kwh=312.0,
                data_available=True,
                data_timestamp_seconds=time.time(),
            ),
            operational_context=OperationalContext(
                current_replicas=2,
                ready_replicas=2,
                availability_ratio=1.0,
                cpu_request_ratio=0.40,
                error_rate_rps=0.0,
                p99_latency_seconds=0.12,
                restart_rate=0.0,
            ),
            metadata=DecisionMetadata(
                confidence=0.85,
                decision_basis="high-carbon-low-load",
                missing_signals=[],
            ),
        ),
        validation=PolicyValidation(
            status=ValidationStatus.APPROVED,
            reason="All safety checks passed.",
            approved_for_gitops_change=True,
            safeguards_triggered=[],
            evaluated_at_seconds=time.time(),
        ),
    )


def make_verifier(
    config: VerificationConfig | None = None,
    gitops_workflow=None,
) -> OptimizationVerifier:
    return OptimizationVerifier(
        config=config or VerificationConfig(
            stabilization_period_seconds=0.0,
            min_deployment_wait_seconds=0.0,
        ),
        gitops_workflow=gitops_workflow,
    )


def make_lifecycle() -> OptimizationLifecycle:
    lc = OptimizationLifecycle()
    lc.emit(LifecycleStage.OBSERVATION, "observation.collected", {})
    lc.emit(LifecycleStage.GITOPS_CHANGE, "gitops.change_prepared", {})
    return lc


# ===========================================================================
# TestWorkloadSnapshot
# ===========================================================================


class TestWorkloadSnapshot:
    def test_from_observation_map_full(self, healthy_post_obs):
        snap = WorkloadSnapshot.from_observation_map(healthy_post_obs)
        assert snap.replica_count_desired == 1.0
        assert snap.availability_ratio == 1.0
        assert snap.http_error_rate_rps == 0.0

    def test_from_observation_map_partial(self):
        snap = WorkloadSnapshot.from_observation_map({"cpu_request_ratio": 0.3})
        assert snap.cpu_request_ratio == 0.3
        assert snap.replica_count_desired is None
        assert snap.http_error_rate_rps is None

    def test_from_observation_map_empty(self):
        snap = WorkloadSnapshot.from_observation_map({})
        assert snap.replica_count_desired is None
        assert snap.availability_ratio is None

    def test_to_dict_is_serializable(self, healthy_pre_snapshot):
        d = healthy_pre_snapshot.to_dict()
        assert isinstance(d, dict)
        assert d["replica_count_desired"] == 2.0
        assert d["http_error_rate_rps"] == 0.0


# ===========================================================================
# TestMetricDelta
# ===========================================================================


class TestMetricDelta:
    def test_basic_decrease(self):
        delta = MetricDelta.compute("replicas", 2.0, 1.0)
        assert delta.delta == pytest.approx(-1.0)
        assert delta.delta_pct == pytest.approx(-0.5)

    def test_basic_increase(self):
        delta = MetricDelta.compute("latency", 0.10, 0.20)
        assert delta.delta == pytest.approx(0.10)
        assert delta.delta_pct == pytest.approx(1.0)

    def test_no_change(self):
        delta = MetricDelta.compute("errors", 0.0, 0.0)
        assert delta.delta == pytest.approx(0.0)
        assert delta.delta_pct is None  # division by zero safeguarded

    def test_before_none(self):
        delta = MetricDelta.compute("x", None, 1.0)
        assert delta.delta is None
        assert delta.delta_pct is None

    def test_after_none(self):
        delta = MetricDelta.compute("x", 1.0, None)
        assert delta.delta is None

    def test_both_none(self):
        delta = MetricDelta.compute("x", None, None)
        assert delta.delta is None

    def test_to_dict_structure(self):
        delta = MetricDelta.compute("cpu", 0.4, 0.6)
        d = delta.to_dict()
        assert d["name"] == "cpu"
        assert d["before"] == 0.4
        assert d["after"] == 0.6
        assert d["delta"] == pytest.approx(0.2)
        assert d["delta_pct"] == pytest.approx(50.0)


class TestComputeDeltas:
    def test_computes_all_fields(self, healthy_pre_snapshot, healthy_post_obs):
        post = WorkloadSnapshot.from_observation_map(healthy_post_obs)
        deltas = compute_deltas(healthy_pre_snapshot, post)
        assert "replica_count_desired" in deltas
        assert "http_error_rate_rps" in deltas
        # Replica count went from 2 to 1
        assert deltas["replica_count_desired"].delta == pytest.approx(-1.0)


# ===========================================================================
# TestVerificationConfig
# ===========================================================================


class TestVerificationConfig:
    def test_defaults_are_sensible(self):
        cfg = VerificationConfig()
        assert cfg.stabilization_period_seconds == 120.0
        assert cfg.rollback_on_error_rate_above == 0.01
        assert cfg.rollback_on_p99_latency_above == 1.0
        assert cfg.rollback_on_availability_below == 1.0
        assert cfg.rollback_on_restart_rate_above == 0.0
        assert cfg.min_required_metrics == 4

    def test_custom_values(self):
        cfg = VerificationConfig(
            stabilization_period_seconds=30.0,
            rollback_on_error_rate_above=0.05,
        )
        assert cfg.stabilization_period_seconds == 30.0
        assert cfg.rollback_on_error_rate_above == 0.05


# ===========================================================================
# TestOptimizationVerifierSuccess
# ===========================================================================


class TestOptimizationVerifierSuccess:
    @pytest.mark.asyncio
    async def test_success_path(
        self,
        healthy_pre_snapshot,
        healthy_post_obs,
        scale_down_validated,
    ):
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return healthy_post_obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )

        assert result.outcome is VerificationOutcome.SUCCESS
        assert result.safety_thresholds_violated == []
        assert result.rollback_prepared is False
        assert result.available_metric_count >= 4

    @pytest.mark.asyncio
    async def test_success_emits_correct_audit_events(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return healthy_post_obs

        await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )

        event_types = {e.event_type for e in lifecycle.audit_events}
        assert "verification.started" in event_types
        assert "verification.metrics_collected" in event_types
        assert "verification.thresholds_evaluated" in event_types
        assert "verification.complete" in event_types

    @pytest.mark.asyncio
    async def test_deltas_captured_in_result(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return healthy_post_obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )

        assert "replica_count_desired" in result.deltas
        replica_delta = result.deltas["replica_count_desired"]
        assert replica_delta["before"] == 2.0
        assert replica_delta["after"] == 1.0


# ===========================================================================
# TestOptimizationVerifierDegraded
# ===========================================================================


class TestOptimizationVerifierDegraded:
    @pytest.mark.asyncio
    async def test_high_cpu_degraded(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "cpu_request_ratio": 0.90}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.DEGRADED
        assert result.rollback_prepared is False
        assert any("CPU" in s for s in result.degradation_signals)


    @pytest.mark.asyncio
    async def test_high_memory_degraded(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "memory_request_ratio": 0.92}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.DEGRADED

    @pytest.mark.asyncio
    async def test_latency_increase_degraded(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        # P99 goes from 0.12s to 0.20s — 67% increase > soft threshold (50%)
        obs = {**healthy_post_obs, "http_p99_latency_seconds": 0.20}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.DEGRADED


# ===========================================================================
# TestOptimizationVerifierRollback
# ===========================================================================


class TestOptimizationVerifierRollback:
    @pytest.mark.asyncio
    async def test_error_rate_triggers_rollback(
        self,
        healthy_pre_snapshot,
        healthy_post_obs,
        scale_down_validated,
    ):
        obs = {**healthy_post_obs, "http_error_rate_rps": 0.05}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED
        assert any("error rate" in v for v in result.safety_thresholds_violated)

    @pytest.mark.asyncio
    async def test_p99_latency_breach_triggers_rollback(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "http_p99_latency_seconds": 1.5}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED
        assert any("P99" in v for v in result.safety_thresholds_violated)

    @pytest.mark.asyncio
    async def test_availability_drop_triggers_rollback(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "pod_availability_ratio": 0.5}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED
        assert any("Availability" in v for v in result.safety_thresholds_violated)

    @pytest.mark.asyncio
    async def test_pod_restart_triggers_rollback(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "pod_restart_rate": 0.01}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED

    @pytest.mark.asyncio
    async def test_rollback_prepared_via_gitops(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        """Verifier should call GitOps workflow to prepare rollback PR."""
        obs = {**healthy_post_obs, "http_error_rate_rps": 0.1}

        fake_gitops = AsyncMock()
        fake_gitops.prepare_change = AsyncMock(
            return_value=GitOpsChangeResult(
                status=GitOpsChangeStatus.PR_CREATED,
                reason="Rollback PR prepared.",
                branch_name="greenops/rollback-branch",
                pull_request_url="https://github.com/test/pr/99",
            )
        )

        verifier = make_verifier(gitops_workflow=fake_gitops)
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )

        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED
        assert result.rollback_prepared is True
        fake_gitops.prepare_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollback_skipped_when_no_gitops(
        self, healthy_pre_snapshot, healthy_post_obs, scale_down_validated
    ):
        obs = {**healthy_post_obs, "http_error_rate_rps": 0.1}
        verifier = OptimizationVerifier(
            config=VerificationConfig(stabilization_period_seconds=0.0),
            gitops_workflow=None,  # no rollback configured
        )
        lifecycle = make_lifecycle()

        async def collector():
            return obs

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.ROLLBACK_REQUIRED
        assert result.rollback_prepared is False


# ===========================================================================
# TestOptimizationVerifierInconclusive
# ===========================================================================


class TestOptimizationVerifierInconclusive:
    @pytest.mark.asyncio
    async def test_collection_exception_is_inconclusive(
        self, healthy_pre_snapshot, scale_down_validated
    ):
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def bad_collector():
            raise ConnectionError("Prometheus unreachable")

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=bad_collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.INCONCLUSIVE
        assert "Prometheus" in result.reason

    @pytest.mark.asyncio
    async def test_sparse_metrics_inconclusive(
        self, healthy_pre_snapshot, scale_down_validated
    ):
        # Only 1 metric — below min_required_metrics=4
        sparse = {"cpu_request_ratio": 0.5}
        verifier = make_verifier()
        lifecycle = make_lifecycle()

        async def collector():
            return sparse

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        assert result.outcome is VerificationOutcome.INCONCLUSIVE

    @pytest.mark.asyncio
    async def test_custom_min_required_metrics(
        self, healthy_pre_snapshot, scale_down_validated
    ):
        sparse = {"cpu_request_ratio": 0.5, "http_error_rate_rps": 0.0}
        config = VerificationConfig(
            stabilization_period_seconds=0.0,
            min_required_metrics=2,  # relaxed
            require_complete_post_change_metrics=False,
            require_deployment_convergence=False,
        )
        verifier = OptimizationVerifier(config=config)
        lifecycle = make_lifecycle()

        async def collector():
            return sparse

        result = await verifier.verify(
            pre_snapshot=healthy_pre_snapshot,
            metric_collector=collector,
            validated=scale_down_validated,
            lifecycle=lifecycle,
            sleep=False,
        )
        # With min=2, 2 metrics is enough to classify
        assert result.outcome is not VerificationOutcome.INCONCLUSIVE


# ===========================================================================
# TestOptimizationLifecycle
# ===========================================================================


class TestOptimizationLifecycle:
    def test_lifecycle_has_unique_id(self):
        lc1 = OptimizationLifecycle()
        lc2 = OptimizationLifecycle()
        assert lc1.lifecycle_id != lc2.lifecycle_id

    def test_emit_appends_event(self):
        lc = OptimizationLifecycle()
        lc.emit(LifecycleStage.OBSERVATION, "test.event", {"key": "value"})
        assert len(lc.audit_events) == 1
        event = lc.audit_events[0]
        assert event.event_type == "test.event"
        assert event.stage is LifecycleStage.OBSERVATION
        assert event.data["key"] == "value"

    def test_events_are_append_only(self):
        lc = OptimizationLifecycle()
        lc.emit(LifecycleStage.OBSERVATION, "obs.done", {})
        lc.emit(LifecycleStage.RECOMMENDATION, "rec.done", {})
        assert len(lc.audit_events) == 2
        assert lc.audit_events[0].stage is LifecycleStage.OBSERVATION
        assert lc.audit_events[1].stage is LifecycleStage.RECOMMENDATION

    def test_complete_sets_final_outcome(self):
        lc = OptimizationLifecycle()
        lc.complete("SUCCESS")
        assert lc.final_outcome == "SUCCESS"
        assert lc.completed_at is not None

    def test_complete_emits_lifecycle_completed_event(self):
        lc = OptimizationLifecycle()
        lc.complete("DEGRADED")
        completion_events = [
            e for e in lc.audit_events if e.event_type == "lifecycle.completed"
        ]
        assert len(completion_events) == 1
        assert completion_events[0].data["outcome"] == "DEGRADED"

    def test_summary_structure(self):
        lc = OptimizationLifecycle()
        lc.emit(LifecycleStage.OBSERVATION, "obs.done", {})
        lc.complete("SUCCESS")
        s = lc.summary()
        assert s["final_outcome"] == "SUCCESS"
        assert s["audit_event_count"] >= 1
        assert "lifecycle_id" in s
        assert "duration_seconds" in s

    def test_audit_event_timestamp_is_set(self):
        before = time.time()
        lc = OptimizationLifecycle()
        lc.emit(LifecycleStage.OBSERVATION, "obs.done", {})
        after = time.time()
        event = lc.audit_events[0]
        assert before <= event.timestamp_seconds <= after

    def test_advance_to_updates_current_stage(self):
        lc = OptimizationLifecycle()
        lc.advance_to(LifecycleStage.RECOMMENDATION)
        assert lc.current_stage is LifecycleStage.RECOMMENDATION


# ===========================================================================
# TestClosedLoopController (end-to-end with mocks)
# ===========================================================================


class TestClosedLoopController:
    """End-to-end controller tests using mock Prometheus and GitOps."""

    def _make_mock_agent(self, validated: ValidatedRecommendation):
        agent = AsyncMock()
        agent.recommend = AsyncMock(return_value=validated)
        return agent

    def _make_mock_prom(self, obs_values: dict[str, float]):
        from monitoring.models import AgentObservation, MetricSnapshot

        snapshots = [
            MetricSnapshot(
                name=k, query=k, value=v,
                labels={}, timestamp=time.time(), unit="",
            )
            for k, v in obs_values.items()
        ]
        observation = AgentObservation(
            snapshots=snapshots,
            collected_at=time.time(),
            namespace="greenops",
            deployment="greenops-demo-workload",
        )
        client = AsyncMock()
        client.collect_agent_observation = AsyncMock(return_value=observation)
        return client

    @pytest.mark.asyncio
    async def test_full_success_lifecycle(self, scale_down_validated):
        from agent.controller import ClosedLoopController

        metrics = {
            "replica_count_desired": 1.0,
            "replica_count_ready": 1.0,
            "pod_availability_ratio": 1.0,
            "cpu_request_ratio": 0.45,
            "memory_request_ratio": 0.55,
            "http_request_rate_rps": 9.0,
            "http_error_rate_rps": 0.0,
            "http_p99_latency_seconds": 0.15,
            "http_p50_latency_seconds": 0.06,
            "pod_restart_rate": 0.0,
        }

        fake_gitops = AsyncMock()
        fake_gitops.prepare_change = AsyncMock(
            return_value=GitOpsChangeResult(
                status=GitOpsChangeStatus.PR_CREATED,
                reason="Branch and PR created.",
                branch_name="greenops/scale-down-to-1",
                commit_sha="abc123",
                pull_request_url="https://github.com/test/pr/1",
            )
        )

        controller = ClosedLoopController(
            prometheus_client=self._make_mock_prom(metrics),
            queries=MagicMock(namespace="greenops", deployment="greenops-demo-workload"),
            decision_agent=self._make_mock_agent(scale_down_validated),
            gitops_workflow=fake_gitops,
            verification_config=VerificationConfig(
                stabilization_period_seconds=0.0,
                min_required_metrics=3,
            ),
        )

        lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)

        assert lifecycle.final_outcome == "SUCCESS"
        assert lifecycle.gitops_status == GitOpsChangeStatus.PR_CREATED
        assert lifecycle.verification_outcome == VerificationOutcome.SUCCESS
        assert lifecycle.rollback_prepared is False
        # Full audit trail must cover all 7 stages
        stages_seen = {e.stage for e in lifecycle.audit_events}
        assert LifecycleStage.OBSERVATION in stages_seen
        assert LifecycleStage.GITOPS_CHANGE in stages_seen
        assert LifecycleStage.VERIFICATION in stages_seen
        assert LifecycleStage.FINAL_RESULT in stages_seen

    @pytest.mark.asyncio
    async def test_rollback_lifecycle(self, scale_down_validated):
        from agent.controller import ClosedLoopController

        pre_metrics = {
            "replica_count_desired": 2.0,
            "replica_count_ready": 2.0,
            "pod_availability_ratio": 1.0,
            "cpu_request_ratio": 0.40,
            "memory_request_ratio": 0.45,
            "http_request_rate_rps": 1.0,
            "http_error_rate_rps": 0.0,
            "http_p99_latency_seconds": 0.12,
            "pod_restart_rate": 0.0,
        }
        post_metrics = {
            **pre_metrics,
            "replica_count_desired": 1.0,
            "replica_count_ready": 1.0,
            "http_error_rate_rps": 0.15,   # violation
            "http_p99_latency_seconds": 1.8,  # violation
        }

        call_count = 0

        async def collect_side_effect(*args, **kwargs):
            from monitoring.models import AgentObservation, MetricSnapshot
            nonlocal call_count
            call_count += 1
            obs = pre_metrics if call_count <= 2 else post_metrics
            return AgentObservation(
                snapshots=[
                    MetricSnapshot(
                        name=k, query=k, value=v,
                        labels={}, timestamp=time.time(), unit="",
                    )
                    for k, v in obs.items()
                ],
                collected_at=time.time(),
                namespace="greenops",
                deployment="greenops-demo-workload",
            )

        prom = AsyncMock()
        prom.collect_agent_observation = collect_side_effect

        rollback_result = GitOpsChangeResult(
            status=GitOpsChangeStatus.PR_CREATED,
            reason="Rollback PR prepared.",
            branch_name="greenops/rollback",
            pull_request_url="https://github.com/test/pr/99",
        )
        original_result = GitOpsChangeResult(
            status=GitOpsChangeStatus.PR_CREATED,
            reason="Branch and PR created.",
            branch_name="greenops/scale-down",
            commit_sha="abc123",
        )

        fake_gitops = AsyncMock()
        fake_gitops.prepare_change = AsyncMock(
            side_effect=[original_result, rollback_result]
        )

        controller = ClosedLoopController(
            prometheus_client=prom,
            queries=MagicMock(namespace="greenops", deployment="greenops-demo-workload"),
            decision_agent=self._make_mock_agent(scale_down_validated),
            gitops_workflow=fake_gitops,
            verification_config=VerificationConfig(
                stabilization_period_seconds=0.0,
                min_required_metrics=3,
            ),
        )

        lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)

        assert lifecycle.final_outcome in {"ROLLBACK_PREPARED", "ROLLBACK_FAILED"}
        assert lifecycle.verification_outcome == VerificationOutcome.ROLLBACK_REQUIRED
        assert lifecycle.rollback_prepared is True
        assert lifecycle.rollback_branch == "greenops/rollback"

    @pytest.mark.asyncio
    async def test_keep_action_short_circuits(self, scale_down_validated):
        from agent.controller import ClosedLoopController

        keep_validated = ValidatedRecommendation(
            recommendation=scale_down_validated.recommendation.model_copy(
                update={"action": Action.KEEP, "recommended_replicas": None}
            ),
            validation=scale_down_validated.validation,
        )

        controller = ClosedLoopController(
            prometheus_client=self._make_mock_prom({}),
            queries=MagicMock(namespace="greenops", deployment="greenops-demo-workload"),
            decision_agent=self._make_mock_agent(keep_validated),
        )

        lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)
        assert lifecycle.final_outcome == "NO_ACTION"

    @pytest.mark.asyncio
    async def test_rejected_policy_short_circuits(self, scale_down_validated):
        from agent.controller import ClosedLoopController

        rejected = ValidatedRecommendation(
            recommendation=scale_down_validated.recommendation,
            validation=PolicyValidation(
                status=ValidationStatus.REJECTED,
                reason="CPU above safety threshold.",
                approved_for_gitops_change=False,
                safeguards_triggered=["CPU utilization is above the scale-down safety threshold"],
                evaluated_at_seconds=time.time(),
            ),
        )

        controller = ClosedLoopController(
            prometheus_client=self._make_mock_prom({}),
            queries=MagicMock(namespace="greenops", deployment="greenops-demo-workload"),
            decision_agent=self._make_mock_agent(rejected),
        )

        lifecycle = await controller.run_optimization_cycle(sleep_for_stabilization=False)
        assert lifecycle.final_outcome is not None
        assert "BLOCKED" in lifecycle.final_outcome
