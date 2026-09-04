"""
agent/verification.py — Post-deployment metric comparison and safety re-evaluation.

After a GitOps optimization change is applied and Kubernetes has stabilized,
the OptimizationVerifier:

  1. Waits a configurable stabilization period.
  2. Queries Prometheus for the current metric state (post-change snapshot).
  3. Compares each metric against the pre-change baseline.
  4. Applies safety thresholds to classify the outcome.
  5. If ROLLBACK_REQUIRED: prepares a restoration change via the GitOps workflow.

Outcomes
--------
SUCCESS          All safety thresholds satisfied; metrics held steady or improved.
DEGRADED         Some metrics worsened but remain within acceptable bounds.
                 No rollback — operator is notified via audit events.
ROLLBACK_REQUIRED One or more hard safety thresholds violated; rollback prepared.
INCONCLUSIVE     Not enough Prometheus data to classify the outcome (e.g. still
                 rolling out, scrape lag).

Design constraints
------------------
- Never modifies Kubernetes directly.
- Rollback is prepared via GitOpsChangeWorkflow with the same safety gates.
- The safety policy is re-evaluated with a relaxed config during rollback
  (availability and error checks still apply; cooldown is waived for emergency).
- All decisions are fully deterministic given the same metric values.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from agent.lifecycle import LifecycleStage, OptimizationLifecycle
from agent.models import (
    Action,
    DecisionMetadata,
    DecisionRecommendation,
    OperationalContext,
    ValidatedRecommendation,
    ValidationStatus,
)
from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
from config import get_logger
from gitops.models import GitOpsChangeResult, GitOpsChangeStatus
from gitops.workflow import GitOpsChangeWorkflow

log = get_logger(__name__)

MetricCollectorFn = Callable[[], Awaitable[dict[str, float]]]


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class VerificationOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    INCONCLUSIVE = "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Metric snapshot (independent of monitoring.models to avoid tight coupling)
# ---------------------------------------------------------------------------


class WorkloadSnapshot(BaseModel):
    """
    Point-in-time workload metrics for pre/post comparison.

    All fields are optional — absent metrics don't block verification
    but do reduce confidence. Missing critical fields can trigger INCONCLUSIVE.
    """

    captured_at: float = Field(default_factory=time.time)

    # Replica state
    replica_count_desired: float | None = None
    replica_count_ready: float | None = None
    availability_ratio: float | None = None

    # Resource utilization
    cpu_request_ratio: float | None = None
    memory_request_ratio: float | None = None

    # Application performance
    http_request_rate_rps: float | None = None
    http_error_rate_rps: float | None = None
    http_p99_latency_seconds: float | None = None
    http_p50_latency_seconds: float | None = None

    # Pod stability
    pod_restart_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_observation_map(cls, obs: dict[str, float]) -> WorkloadSnapshot:
        """Build a snapshot from a flat metric-name → value mapping."""
        return cls(
            replica_count_desired=obs.get("replica_count_desired"),
            replica_count_ready=obs.get("replica_count_ready"),
            availability_ratio=obs.get("pod_availability_ratio"),
            cpu_request_ratio=obs.get("cpu_request_ratio"),
            memory_request_ratio=obs.get("memory_request_ratio"),
            http_request_rate_rps=obs.get("http_request_rate_rps"),
            http_error_rate_rps=obs.get("http_error_rate_rps"),
            http_p99_latency_seconds=obs.get("http_p99_latency_seconds"),
            http_p50_latency_seconds=obs.get("http_p50_latency_seconds"),
            pod_restart_rate=obs.get("pod_restart_rate"),
        )


# ---------------------------------------------------------------------------
# Metric delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDelta:
    """Absolute and percentage change between pre and post snapshots."""

    name: str
    before: float | None
    after: float | None
    delta: float | None  # after - before, None if either is missing
    delta_pct: float | None  # (after - before) / before, None if before is 0 or missing

    @classmethod
    def compute(cls, name: str, before: float | None, after: float | None) -> MetricDelta:
        delta: float | None = None
        delta_pct: float | None = None
        if before is not None and after is not None:
            delta = after - before
            if before != 0.0:
                delta_pct = delta / abs(before)
        return cls(name=name, before=before, after=after, delta=delta, delta_pct=delta_pct)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "before": self.before,
            "after": self.after,
            "delta": round(self.delta, 6) if self.delta is not None else None,
            "delta_pct": round(self.delta_pct * 100, 2) if self.delta_pct is not None else None,
        }


def compute_deltas(pre: WorkloadSnapshot, post: WorkloadSnapshot) -> dict[str, MetricDelta]:
    """Compute MetricDelta for every field present in both snapshots."""
    fields = [
        "replica_count_desired",
        "replica_count_ready",
        "availability_ratio",
        "cpu_request_ratio",
        "memory_request_ratio",
        "http_request_rate_rps",
        "http_error_rate_rps",
        "http_p99_latency_seconds",
        "http_p50_latency_seconds",
        "pod_restart_rate",
    ]
    return {
        f: MetricDelta.compute(
            f,
            getattr(pre, f, None),
            getattr(post, f, None),
        )
        for f in fields
    }


# ---------------------------------------------------------------------------
# Verification configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationConfig:
    """
    Configurable thresholds that determine when a post-change state is safe.

    Hard thresholds → ROLLBACK_REQUIRED when violated.
    Soft thresholds → DEGRADED when violated (no automatic rollback).
    """

    # ── Stabilization ───────────────────────────────────────────────────
    stabilization_period_seconds: float = 120.0
    """Time to wait after GitOps change before collecting post-change metrics."""

    min_deployment_wait_seconds: float = 30.0
    """Minimum wait before checking whether replicas have converged."""

    max_inconclusive_wait_seconds: float = 300.0
    """Maximum time to wait for replica convergence before declaring INCONCLUSIVE."""

    # ── Hard thresholds (ROLLBACK_REQUIRED) ─────────────────────────────
    rollback_on_error_rate_above: float = 0.01
    """Trigger rollback if HTTP error rate (rps) exceeds this value after optimization."""

    rollback_on_p99_latency_above: float = 1.0
    """Trigger rollback if P99 latency (seconds) exceeds this threshold."""

    rollback_on_availability_below: float = 1.0
    """Trigger rollback if pod availability ratio drops below this value."""

    rollback_on_restart_rate_above: float = 0.0
    """Trigger rollback if any container restarts are observed post-change."""

    # ── Soft thresholds (DEGRADED only) ─────────────────────────────────
    degrade_on_cpu_ratio_above: float = 0.80
    """Report DEGRADED (not rollback) if CPU request ratio rises above this."""

    degrade_on_memory_ratio_above: float = 0.85
    """Report DEGRADED (not rollback) if memory request ratio rises above this."""

    degrade_on_latency_increase_pct: float = 0.50
    """Report DEGRADED if P99 latency increases by more than this fraction."""

    # ── Confidence thresholds ───────────────────────────────────────────
    min_required_metrics: int = 4
    """Minimum number of post-change metrics required to avoid INCONCLUSIVE."""

    require_complete_post_change_metrics: bool = True
    """Require the full CPU/memory/latency/traffic/health/replica metric set."""

    require_deployment_convergence: bool = True
    """Require observed Kubernetes replicas to match the GitOps target."""


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


class VerificationResult(BaseModel):
    """Outcome of a single post-deployment verification cycle."""

    outcome: VerificationOutcome
    reason: str

    pre_snapshot: WorkloadSnapshot
    post_snapshot: WorkloadSnapshot
    deltas: dict[str, dict[str, Any]] = Field(default_factory=dict)

    stabilization_waited_seconds: float = 0.0
    safety_thresholds_violated: list[str] = Field(default_factory=list)
    degradation_signals: list[str] = Field(default_factory=list)
    available_metric_count: int = 0

    rollback_prepared: bool = False
    rollback_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class OptimizationVerifier:
    """
    Verifies whether a GreenOps optimization achieved its intended result.

    The verifier:
      1. Waits ``config.stabilization_period_seconds`` after the GitOps change.
      2. Collects post-change metrics from Prometheus via ``metric_collector``.
      3. Compares every metric to the pre-change ``WorkloadSnapshot``.
      4. Classifies the outcome (SUCCESS / DEGRADED / ROLLBACK_REQUIRED / INCONCLUSIVE).
      5. If ROLLBACK_REQUIRED: prepares a restoration change through GitOpsChangeWorkflow.

    ``metric_collector`` is a callable ``async (metric_names) -> dict[name, float]``
    so the verifier is fully testable without a real Prometheus instance.
    """

    _REQUIRED_POST_CHANGE_FIELDS = (
        "replica_count_desired",
        "replica_count_ready",
        "availability_ratio",
        "cpu_request_ratio",
        "memory_request_ratio",
        "http_request_rate_rps",
        "http_error_rate_rps",
        "http_p99_latency_seconds",
        "pod_restart_rate",
    )

    def __init__(
        self,
        config: VerificationConfig | None = None,
        gitops_workflow: GitOpsChangeWorkflow | None = None,
    ) -> None:
        self.config = config or VerificationConfig()
        self._gitops = gitops_workflow

    async def verify(
        self,
        *,
        pre_snapshot: WorkloadSnapshot,
        metric_collector: MetricCollectorFn,
        validated: ValidatedRecommendation,
        lifecycle: OptimizationLifecycle,
        sleep: bool = True,
    ) -> VerificationResult:
        """
        Run a full verification cycle.

        Args:
            pre_snapshot:      Metrics captured immediately before the GitOps change.
            metric_collector:  Async callable that returns a dict of metric values.
            validated:         The original validated recommendation (used for rollback).
            lifecycle:         The parent lifecycle (receives audit events).
            sleep:             Set False in tests to skip real-time waits.

        Returns:
            VerificationResult with outcome and any rollback details.
        """
        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.started",
            {
                "stabilization_period_seconds": self.config.stabilization_period_seconds,
                "pre_replicas": pre_snapshot.replica_count_desired,
            },
        )

        # ── Wait for stabilization ───────────────────────────────────────
        wait_started = time.time()
        if sleep:
            log.info(
                "verification.stabilizing",
                seconds=self.config.stabilization_period_seconds,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            await asyncio.sleep(self.config.stabilization_period_seconds)
        waited = time.time() - wait_started

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.stabilization_complete",
            {"waited_seconds": round(waited, 1)},
        )

        # ── Collect post-change metrics ──────────────────────────────────
        try:
            obs = await metric_collector()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "verification.collection_failed",
                error=str(exc),
                lifecycle_id=lifecycle.lifecycle_id,
            )
            lifecycle.emit(
                LifecycleStage.VERIFICATION,
                "verification.collection_failed",
                {"error": str(exc)},
            )
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason=f"Prometheus metric collection failed during verification: {exc}",
                pre_snapshot=pre_snapshot,
                post_snapshot=WorkloadSnapshot(),
                stabilization_waited_seconds=round(waited, 1),
            )

        post_snapshot = WorkloadSnapshot.from_observation_map(obs)
        available = sum(
            1
            for f in self._REQUIRED_POST_CHANGE_FIELDS
            if getattr(post_snapshot, f, None) is not None
        )
        missing_required = [
            f for f in self._REQUIRED_POST_CHANGE_FIELDS if getattr(post_snapshot, f, None) is None
        ]

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.metrics_collected",
            {
                "available_metric_count": available,
                "missing_required_metrics": missing_required,
                "post_replicas": post_snapshot.replica_count_desired,
            },
        )

        # ── Insufficient data ────────────────────────────────────────────
        if self.config.require_complete_post_change_metrics and missing_required:
            log.warning(
                "verification.inconclusive",
                missing_required_metrics=missing_required,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason=(
                    "Post-change Prometheus data is missing required verification "
                    "metrics: " + ", ".join(missing_required) + "."
                ),
                pre_snapshot=pre_snapshot,
                post_snapshot=post_snapshot,
                stabilization_waited_seconds=round(waited, 1),
                available_metric_count=available,
            )

        deployment_gap = (
            self._deployment_convergence_gap(post_snapshot, validated)
            if self.config.require_deployment_convergence
            else None
        )
        if deployment_gap is not None:
            log.warning(
                "verification.inconclusive",
                reason=deployment_gap,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            lifecycle.emit(
                LifecycleStage.VERIFICATION,
                "verification.deployment_not_converged",
                {"reason": deployment_gap},
            )
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason=deployment_gap,
                pre_snapshot=pre_snapshot,
                post_snapshot=post_snapshot,
                stabilization_waited_seconds=round(waited, 1),
                available_metric_count=available,
            )

        if available < self.config.min_required_metrics:
            log.warning(
                "verification.inconclusive",
                available=available,
                required=self.config.min_required_metrics,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return VerificationResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason=(
                    f"Only {available} of {self.config.min_required_metrics} required "
                    "metrics available after stabilization."
                ),
                pre_snapshot=pre_snapshot,
                post_snapshot=post_snapshot,
                stabilization_waited_seconds=round(waited, 1),
                available_metric_count=available,
            )

        # ── Compute deltas ───────────────────────────────────────────────
        deltas = compute_deltas(pre_snapshot, post_snapshot)
        deltas_json = {k: v.to_dict() for k, v in deltas.items()}

        # ── Safety threshold evaluation ──────────────────────────────────
        violations = self._hard_violations(post_snapshot)
        degradations = self._soft_degradations(post_snapshot, deltas)

        log.info(
            "verification.evaluated",
            violations=violations,
            degradations=degradations,
            lifecycle_id=lifecycle.lifecycle_id,
        )

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.thresholds_evaluated",
            {
                "hard_violations": violations,
                "soft_degradations": degradations,
                "delta_summary": {
                    k: {"before": v.before, "after": v.after, "delta_pct": v.delta_pct}
                    for k, v in deltas.items()
                    if v.before is not None or v.after is not None
                },
            },
        )

        # ── Classify outcome ─────────────────────────────────────────────
        if violations:
            outcome = VerificationOutcome.ROLLBACK_REQUIRED
            reason = (
                "Hard safety thresholds violated after optimization: " + "; ".join(violations) + "."
            )
        elif degradations:
            outcome = VerificationOutcome.DEGRADED
            reason = (
                "Optimization applied; performance degradation detected (no rollback): "
                + "; ".join(degradations)
                + "."
            )
        else:
            outcome = VerificationOutcome.SUCCESS
            reason = (
                "Optimization verified: all safety thresholds satisfied. "
                f"Replicas: {pre_snapshot.replica_count_desired} → "
                f"{post_snapshot.replica_count_desired}."
            )

        result = VerificationResult(
            outcome=outcome,
            reason=reason,
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            deltas=deltas_json,
            stabilization_waited_seconds=round(waited, 1),
            safety_thresholds_violated=violations,
            degradation_signals=degradations,
            available_metric_count=available,
        )

        # ── Rollback if required ─────────────────────────────────────────
        if outcome is VerificationOutcome.ROLLBACK_REQUIRED and self._gitops is not None:
            result = await self._prepare_rollback(result, validated, lifecycle)

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.complete",
            {
                "outcome": outcome,
                "rollback_prepared": result.rollback_prepared,
                "violations": violations,
            },
        )

        log.info(
            "verification.result",
            outcome=outcome,
            reason=reason,
            rollback_prepared=result.rollback_prepared,
            lifecycle_id=lifecycle.lifecycle_id,
        )
        return result

    def _deployment_convergence_gap(
        self,
        post: WorkloadSnapshot,
        validated: ValidatedRecommendation,
    ) -> str | None:
        target = validated.recommendation.recommended_replicas
        if target is None:
            return None
        desired = post.replica_count_desired
        ready = post.replica_count_ready
        if desired is None:
            return "Post-change replica count is unavailable; Argo CD/Kubernetes convergence cannot be verified."
        if int(round(desired)) != target:
            return (
                "Post-change desired replicas have not converged to the GitOps target "
                f"({desired:g} observed, {target} expected)."
            )
        if ready is None:
            return "Post-change ready replica count is unavailable; workload readiness cannot be verified."
        if int(round(ready)) != int(round(desired)):
            return (
                "Post-change ready replicas have not converged to desired replicas "
                f"({ready:g} ready, {desired:g} desired)."
            )
        return None

    # -----------------------------------------------------------------------
    # Hard violation checks (→ ROLLBACK_REQUIRED)
    # -----------------------------------------------------------------------

    def _hard_violations(self, post: WorkloadSnapshot) -> list[str]:
        violations: list[str] = []
        cfg = self.config

        err = post.http_error_rate_rps
        if err is not None and err > cfg.rollback_on_error_rate_above:
            violations.append(
                f"HTTP error rate {err:.4f} rps > threshold {cfg.rollback_on_error_rate_above} rps"
            )

        p99 = post.http_p99_latency_seconds
        if p99 is not None and p99 > cfg.rollback_on_p99_latency_above:
            violations.append(f"P99 latency {p99:.3f}s > SLA {cfg.rollback_on_p99_latency_above}s")

        avail = post.availability_ratio
        if avail is not None and avail < cfg.rollback_on_availability_below:
            violations.append(
                f"Availability ratio {avail:.3f} < threshold {cfg.rollback_on_availability_below}"
            )

        restarts = post.pod_restart_rate
        if restarts is not None and restarts > cfg.rollback_on_restart_rate_above:
            violations.append(
                f"Pod restart rate {restarts:.6f}/s > threshold {cfg.rollback_on_restart_rate_above}/s"
            )

        return violations

    # -----------------------------------------------------------------------
    # Soft degradation checks (→ DEGRADED, no rollback)
    # -----------------------------------------------------------------------

    def _soft_degradations(
        self,
        post: WorkloadSnapshot,
        deltas: dict[str, MetricDelta],
    ) -> list[str]:
        degradations: list[str] = []
        cfg = self.config

        cpu = post.cpu_request_ratio
        if cpu is not None and cpu > cfg.degrade_on_cpu_ratio_above:
            degradations.append(
                f"CPU request ratio {cpu:.3f} > soft threshold {cfg.degrade_on_cpu_ratio_above}"
            )

        mem = post.memory_request_ratio
        if mem is not None and mem > cfg.degrade_on_memory_ratio_above:
            degradations.append(
                f"Memory request ratio {mem:.3f} > soft threshold {cfg.degrade_on_memory_ratio_above}"
            )

        latency_delta = deltas.get("http_p99_latency_seconds")
        if (
            latency_delta is not None
            and latency_delta.delta_pct is not None
            and latency_delta.delta_pct > cfg.degrade_on_latency_increase_pct
        ):
            pct = round(latency_delta.delta_pct * 100, 1)
            degradations.append(
                f"P99 latency increased {pct}% > soft threshold "
                f"{cfg.degrade_on_latency_increase_pct * 100:.0f}%"
            )

        return degradations

    # -----------------------------------------------------------------------
    # Rollback preparation
    # -----------------------------------------------------------------------

    async def _prepare_rollback(
        self,
        result: VerificationResult,
        original: ValidatedRecommendation,
        lifecycle: OptimizationLifecycle,
    ) -> VerificationResult:
        """Prepare a GitOps change that restores the previous replica count."""
        original_rec = original.recommendation
        pre_replicas = result.pre_snapshot.replica_count_desired
        post_replicas = result.post_snapshot.replica_count_desired

        if pre_replicas is None:
            lifecycle.emit(
                LifecycleStage.VERIFICATION,
                "verification.rollback_skipped",
                {"reason": "pre-change replica count not available"},
            )
            log.warning(
                "verification.rollback_skipped",
                reason="pre_replicas is None",
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return result.model_copy(update={"rollback_prepared": False})

        restore_replicas = int(pre_replicas)
        rollback_action = (
            Action.SCALE_UP if (post_replicas or 0) < restore_replicas else Action.SCALE_DOWN
        )

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.rollback_initiated",
            {
                "restore_to_replicas": restore_replicas,
                "from_replicas": post_replicas,
                "rollback_action": rollback_action,
                "violations": result.safety_thresholds_violated,
            },
        )
        log.warning(
            "verification.rollback_initiated",
            restore_to=restore_replicas,
            violations=result.safety_thresholds_violated,
            lifecycle_id=lifecycle.lifecycle_id,
        )

        # Build a restoration recommendation using the emergency safety config
        # (cooldown waived; all other checks still apply).
        rollback_rec = DecisionRecommendation(
            action=rollback_action,
            current_replicas=int(post_replicas) if post_replicas is not None else None,
            recommended_replicas=restore_replicas,
            reason=(
                f"Emergency restoration: verification detected safety violations "
                f"({'; '.join(result.safety_thresholds_violated)}). "
                f"Restoring to {restore_replicas} replicas."
            ),
            environmental_context=original_rec.environmental_context,
            operational_context=OperationalContext(
                current_replicas=int(post_replicas) if post_replicas is not None else None,
                ready_replicas=int(result.post_snapshot.replica_count_ready or 0),
                availability_ratio=result.post_snapshot.availability_ratio,
                cpu_request_ratio=result.post_snapshot.cpu_request_ratio,
                memory_request_ratio=result.post_snapshot.memory_request_ratio,
                request_rate_rps=result.post_snapshot.http_request_rate_rps,
                error_rate_rps=result.post_snapshot.http_error_rate_rps,
                p99_latency_seconds=result.post_snapshot.http_p99_latency_seconds,
                restart_rate=result.post_snapshot.pod_restart_rate,
            ),
            metadata=DecisionMetadata(
                policy_version="phase-10-rollback-v1",
                read_only=True,
                confidence=1.0,
                decision_basis="emergency-rollback",
                missing_signals=[],
            ),
        )

        # Validate the rollback through the safety policy
        # Use an emergency config: cooldown waived, but SLA checks still enforced.
        emergency_config = OptimizationSafetyConfig(
            min_replicas=1,
            max_replicas=10,
            cpu_safety_threshold=0.95,  # relaxed for emergency
            latency_sla_threshold_seconds=2.0,  # relaxed for emergency
            require_all_replicas_ready=False,  # can't require health during degradation
            reject_on_http_errors=False,  # errors are why we're rolling back
            reject_on_restarts=False,  # restarts are why we're rolling back
            max_scale_down_percentage=1.0,  # allow full restoration
            cooldown_seconds=0.0,  # waived for emergency
            max_carbon_data_age_seconds=3600.0,  # relaxed; carbon data may be old
            require_health_evidence_for_scale_down=False,  # degraded telemetry must not block a rollback
            min_confidence_for_auto_approval=0.0,  # deterministic rollback, not a heuristic
        )
        rollback_validation = OptimizationSafetyPolicy(emergency_config).validate(
            rollback_rec,
            now=time.time(),
            last_optimization_timestamp_seconds=None,
        )

        if rollback_validation.status is not ValidationStatus.APPROVED:
            lifecycle.emit(
                LifecycleStage.VERIFICATION,
                "verification.rollback_policy_blocked",
                {"reason": rollback_validation.reason},
            )
            log.error(
                "verification.rollback_blocked_by_policy",
                reason=rollback_validation.reason,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return result.model_copy(
                update={
                    "rollback_prepared": False,
                    "rollback_result": {
                        "blocked": True,
                        "reason": rollback_validation.reason,
                    },
                }
            )

        rollback_validated = ValidatedRecommendation(
            recommendation=rollback_rec,
            validation=rollback_validation,
        )

        assert self._gitops is not None
        try:
            rollback_gitops: GitOpsChangeResult = await self._gitops.prepare_change(
                rollback_validated
            )
        except Exception as exc:  # noqa: BLE001
            lifecycle.emit(
                LifecycleStage.VERIFICATION,
                "verification.rollback_gitops_failed",
                {"error": str(exc)},
            )
            log.error(
                "verification.rollback_gitops_error",
                error=str(exc),
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return result.model_copy(
                update={
                    "rollback_prepared": False,
                    "rollback_result": {"error": str(exc)},
                }
            )

        lifecycle.emit(
            LifecycleStage.VERIFICATION,
            "verification.rollback_prepared",
            {
                "rollback_status": rollback_gitops.status,
                "rollback_branch": rollback_gitops.branch_name,
                "rollback_pr_url": rollback_gitops.pull_request_url,
            },
        )

        rollback_success = rollback_gitops.status in {
            GitOpsChangeStatus.PREPARED,
            GitOpsChangeStatus.PR_CREATED,
        }

        return result.model_copy(
            update={
                "rollback_prepared": rollback_success,
                "rollback_result": {
                    "status": rollback_gitops.status,
                    "branch": rollback_gitops.branch_name,
                    "commit_sha": rollback_gitops.commit_sha,
                    "pr_url": rollback_gitops.pull_request_url,
                    "reason": rollback_gitops.reason,
                },
            }
        )


# ---------------------------------------------------------------------------
# Type alias for the metric collector dependency
# ---------------------------------------------------------------------------

# Callers supply this as an async callable returning {metric_name: float}.
# Typical implementation wraps PrometheusClient.collect_agent_observation().
