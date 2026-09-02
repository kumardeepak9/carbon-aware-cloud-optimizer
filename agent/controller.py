"""
agent/controller.py — Closed-loop GreenOps optimization controller.

Orchestrates the complete seven-stage optimization lifecycle:

    OBSERVATION → RECOMMENDATION → POLICY_VALIDATION → GITOPS_CHANGE
    → DEPLOYMENT → VERIFICATION → FINAL_RESULT

Each stage emits structured audit events to the OptimizationLifecycle, creating
a permanent, traceable record from carbon observation to verification outcome.

Deployment path (unchanged from Phase 8):

    AI Agent → OptimizationSafetyPolicy → GitOpsChangeWorkflow
        → GitHub PR (human review) → Argo CD sync → Kubernetes
        → [stabilize] → OptimizationVerifier → FINAL_RESULT

The controller never modifies Kubernetes directly.
"""

from __future__ import annotations

import time
from typing import Any

from agent.lifecycle import LifecycleStage, OptimizationLifecycle
from agent.models import (
    Action,
    ValidatedRecommendation,
    ValidationStatus,
)
from agent.safety import OptimizationSafetyConfig, OptimizationSafetyPolicy
from agent.service import GreenOpsDecisionAgent
from agent.verification import (
    OptimizationVerifier,
    VerificationConfig,
    VerificationOutcome,
    VerificationResult,
    WorkloadSnapshot,
)
from config import get_logger
from gitops.models import GitOpsChangeStatus
from gitops.workflow import GitOpsChangeWorkflow
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ClosedLoopController:
    """
    Runs the complete GreenOps optimization loop for a single cycle.

    Usage — standalone (programmatic)::

        async with PrometheusClient(base_url=...) as prom:
            controller = ClosedLoopController(
                prometheus_client=prom,
                queries=GreenOpsQueries(),
                gitops_workflow=GitOpsChangeWorkflow(),
            )
            lifecycle = await controller.run_optimization_cycle()
            print(lifecycle.summary())

    The controller is designed to be called from a scheduler (e.g. APScheduler)
    once per poll interval. Each call creates a fresh OptimizationLifecycle.
    """

    def __init__(
        self,
        *,
        prometheus_client: PrometheusClient,
        queries: GreenOpsQueries,
        decision_agent: GreenOpsDecisionAgent | None = None,
        safety_config: OptimizationSafetyConfig | None = None,
        gitops_workflow: GitOpsChangeWorkflow | None = None,
        verification_config: VerificationConfig | None = None,
        last_optimization_timestamp: float | None = None,
        history_store: Any | None = None,
    ) -> None:
        self._prom = prometheus_client
        self._queries = queries
        # Optional append-only audit sink (chat.history.DecisionHistoryStore).
        # When set, every completed lifecycle is flattened and appended so the
        # chat interface can answer historical questions from real records.
        self._history_store = history_store
        self._safety_policy = OptimizationSafetyPolicy(safety_config or OptimizationSafetyConfig())
        self._agent = decision_agent or GreenOpsDecisionAgent(
            prometheus_client,
            safety_policy=self._safety_policy,
        )
        self._gitops = gitops_workflow
        if self._gitops is not None:
            # Ensure the workflow's independent boundary re-validation uses the
            # same thresholds this controller was configured with.
            self._gitops.safety_policy = self._safety_policy
        self._verifier = OptimizationVerifier(
            config=verification_config or VerificationConfig(),
            gitops_workflow=gitops_workflow,
        )
        self.last_optimization_timestamp = last_optimization_timestamp

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    async def run_optimization_cycle(
        self,
        *,
        sleep_for_stabilization: bool = True,
    ) -> OptimizationLifecycle:
        """
        Execute a complete optimization lifecycle from observation to final result.

        Args:
            sleep_for_stabilization: Set False in tests to skip asyncio.sleep.

        Returns:
            The completed OptimizationLifecycle with full audit trail.
        """
        lifecycle = OptimizationLifecycle()
        log.info(
            "controller.cycle_started",
            lifecycle_id=lifecycle.lifecycle_id,
            namespace=self._queries.namespace,
            deployment=self._queries.deployment,
        )

        try:
            await self._stage_observation(lifecycle)
            validated = await self._stage_recommendation(lifecycle)
            if validated is None:
                return lifecycle

            gitops_result, pre_snapshot = await self._stage_gitops(lifecycle, validated)
            if gitops_result is None:
                return lifecycle

            # Record optimization timestamp for cooldown tracking
            self.last_optimization_timestamp = time.time()

            await self._stage_deployment_wait(lifecycle, sleep_for_stabilization)

            verification = await self._stage_verification(
                lifecycle,
                validated=validated,
                pre_snapshot=pre_snapshot,
                sleep=sleep_for_stabilization,
            )

            self._stage_final_result(lifecycle, validation=validated, verification=verification)

        except Exception as exc:  # noqa: BLE001
            log.error(
                "controller.cycle_error",
                error=str(exc),
                lifecycle_id=lifecycle.lifecycle_id,
                exc_info=True,
            )
            lifecycle.emit(
                LifecycleStage.FINAL_RESULT,
                "lifecycle.error",
                {"error": str(exc), "stage": lifecycle.current_stage},
            )
            lifecycle.complete("ERROR")
        finally:
            # Persist every terminal lifecycle — including DEFERRED / BLOCKED
            # (policy REJECTED) cycles that short-circuit above, so the chat
            # interface can answer "were any recommendations rejected".
            self._persist(lifecycle)

        log.info(
            "controller.cycle_complete",
            **lifecycle.summary(),
        )
        return lifecycle

    def _persist(self, lifecycle: OptimizationLifecycle) -> None:
        """Append the completed lifecycle to the decision-history store, if set.

        Never raises: an audit-sink failure must not fail an optimization cycle.
        """
        if self._history_store is None:
            return
        try:
            from chat.history import record_lifecycle

            record_lifecycle(self._history_store, lifecycle)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "controller.history_persist_failed",
                error=str(exc),
                lifecycle_id=lifecycle.lifecycle_id,
            )

    # -----------------------------------------------------------------------
    # Stage 1: Observation
    # -----------------------------------------------------------------------

    async def _stage_observation(self, lifecycle: OptimizationLifecycle) -> None:
        """Collect current Prometheus metrics — environmental + operational."""
        observation = await self._prom.collect_agent_observation(
            self._queries,
            namespace=self._queries.namespace,
            deployment=self._queries.deployment,
        )

        snapshot_map = {s.name: s.value for s in observation.snapshots}
        lifecycle.emit(
            LifecycleStage.OBSERVATION,
            "observation.collected",
            {
                "metric_count": len(observation.snapshots),
                "collected_at": observation.collected_at,
                "carbon_intensity": snapshot_map.get("carbon_intensity_gco2_kwh"),
                "cpu_request_ratio": snapshot_map.get("cpu_request_ratio"),
                "replica_count_desired": snapshot_map.get("replica_count_desired"),
                "availability_ratio": snapshot_map.get("pod_availability_ratio"),
            },
        )
        log.info(
            "controller.observation_complete",
            metrics=len(observation.snapshots),
            lifecycle_id=lifecycle.lifecycle_id,
        )

    # -----------------------------------------------------------------------
    # Stage 2 + 3: Recommendation + Policy Validation
    # -----------------------------------------------------------------------

    async def _stage_recommendation(
        self,
        lifecycle: OptimizationLifecycle,
    ) -> ValidatedRecommendation | None:
        """Run the AI agent and safety policy; short-circuit on DEFER or REJECTED."""
        lifecycle.advance_to(LifecycleStage.RECOMMENDATION)

        validated: ValidatedRecommendation = await self._agent.recommend(
            self._queries,
            last_optimization_timestamp_seconds=self.last_optimization_timestamp,
        )

        rec = validated.recommendation
        val = validated.validation

        lifecycle.recommendation_json = rec.model_dump(mode="json")
        lifecycle.emit(
            LifecycleStage.RECOMMENDATION,
            "recommendation.produced",
            {
                "action": rec.action,
                "current_replicas": rec.current_replicas,
                "recommended_replicas": rec.recommended_replicas,
                "confidence": rec.metadata.confidence,
                "reason": rec.reason,
            },
        )

        lifecycle.validation_json = val.model_dump(mode="json")
        lifecycle.emit(
            LifecycleStage.POLICY_VALIDATION,
            "policy_validation.evaluated",
            {
                "status": val.status,
                "approved_for_gitops": val.approved_for_gitops_change,
                "reason": val.reason,
                "safeguards_triggered": val.safeguards_triggered,
            },
        )

        if val.status is ValidationStatus.REJECTED:
            lifecycle.complete(f"BLOCKED:{val.status}")
            log.info(
                "controller.blocked_by_policy",
                status=val.status,
                reason=val.reason,
                lifecycle_id=lifecycle.lifecycle_id,
            )
            return None

        if rec.action is Action.DEFER:
            lifecycle.complete("DEFERRED")
            return None

        if rec.action is Action.KEEP:
            lifecycle.complete("NO_ACTION")
            return None

        return validated

    # -----------------------------------------------------------------------
    # Stage 4: GitOps Change
    # -----------------------------------------------------------------------

    async def _stage_gitops(
        self,
        lifecycle: OptimizationLifecycle,
        validated: ValidatedRecommendation,
    ) -> tuple[Any, WorkloadSnapshot]:
        """
        Collect pre-change snapshot, then prepare the GitOps branch + PR.

        Returns (gitops_result, pre_snapshot). Returns (None, snapshot) if
        no GitOps workflow is configured or the change was blocked.
        """
        lifecycle.advance_to(LifecycleStage.GITOPS_CHANGE)

        # Capture pre-change baseline
        pre_obs = await self._prom.collect_agent_observation(
            self._queries,
            namespace=self._queries.namespace,
            deployment=self._queries.deployment,
        )
        pre_map = {s.name: s.value for s in pre_obs.snapshots}
        pre_snapshot = WorkloadSnapshot.from_observation_map(pre_map)

        lifecycle.pre_snapshot_json = pre_snapshot.to_dict()
        lifecycle.emit(
            LifecycleStage.GITOPS_CHANGE,
            "gitops.pre_snapshot_captured",
            {
                "replicas": pre_snapshot.replica_count_desired,
                "cpu_request_ratio": pre_snapshot.cpu_request_ratio,
                "availability_ratio": pre_snapshot.availability_ratio,
            },
        )

        if self._gitops is None:
            lifecycle.emit(
                LifecycleStage.GITOPS_CHANGE,
                "gitops.skipped",
                {"reason": "No GitOps workflow configured (read-only mode)."},
            )
            lifecycle.complete("READ_ONLY")
            return None, pre_snapshot

        try:
            gitops_result = await self._gitops.prepare_change(validated)
        except Exception as exc:  # noqa: BLE001
            lifecycle.emit(
                LifecycleStage.GITOPS_CHANGE,
                "gitops.error",
                {"error": str(exc)},
            )
            lifecycle.complete("GITOPS_ERROR")
            return None, pre_snapshot

        lifecycle.gitops_status = gitops_result.status
        lifecycle.gitops_branch = gitops_result.branch_name
        lifecycle.gitops_pr_url = gitops_result.pull_request_url
        lifecycle.gitops_commit_sha = gitops_result.commit_sha

        lifecycle.emit(
            LifecycleStage.GITOPS_CHANGE,
            "gitops.change_prepared",
            {
                "status": gitops_result.status,
                "branch": gitops_result.branch_name,
                "commit_sha": gitops_result.commit_sha,
                "pr_url": gitops_result.pull_request_url,
                "reason": gitops_result.reason,
            },
        )

        if gitops_result.status is GitOpsChangeStatus.BLOCKED:
            lifecycle.complete(f"GITOPS_BLOCKED:{gitops_result.reason[:60]}")
            return None, pre_snapshot

        if gitops_result.status is GitOpsChangeStatus.NO_OP:
            lifecycle.complete("NO_OP")
            return None, pre_snapshot

        return gitops_result, pre_snapshot

    # -----------------------------------------------------------------------
    # Stage 5: Deployment wait
    # -----------------------------------------------------------------------

    async def _stage_deployment_wait(
        self,
        lifecycle: OptimizationLifecycle,
        sleep: bool,
    ) -> None:
        """
        Note: In production, Argo CD syncs after a human merges the PR.
        This stage records the wait intent and timestamp; actual Kubernetes
        reconciliation is observed indirectly via the verification Prometheus queries.
        """
        lifecycle.advance_to(LifecycleStage.DEPLOYMENT)
        lifecycle.deployment_confirmed_at = time.time()
        lifecycle.emit(
            LifecycleStage.DEPLOYMENT,
            "deployment.wait_recorded",
            {
                "note": (
                    "GitOps PR prepared. Argo CD syncs after human PR review + merge. "
                    "Verification will query Prometheus to observe actual replica state."
                ),
                "deployment_confirmed_at": lifecycle.deployment_confirmed_at,
            },
        )

    # -----------------------------------------------------------------------
    # Stage 6: Verification
    # -----------------------------------------------------------------------

    async def _stage_verification(
        self,
        lifecycle: OptimizationLifecycle,
        *,
        validated: ValidatedRecommendation,
        pre_snapshot: WorkloadSnapshot,
        sleep: bool,
    ) -> VerificationResult:
        """Collect post-change metrics and classify the outcome."""

        async def metric_collector() -> dict[str, float]:
            obs = await self._prom.collect_agent_observation(
                self._queries,
                namespace=self._queries.namespace,
                deployment=self._queries.deployment,
            )
            return {s.name: s.value for s in obs.snapshots}

        result = await self._verifier.verify(
            pre_snapshot=pre_snapshot,
            metric_collector=metric_collector,
            validated=validated,
            lifecycle=lifecycle,
            sleep=sleep,
        )

        lifecycle.verification_outcome = result.outcome
        lifecycle.verification_reason = result.reason
        lifecycle.post_snapshot_json = result.post_snapshot.to_dict()
        lifecycle.metric_deltas_json = result.deltas
        lifecycle.safety_thresholds_violated = result.safety_thresholds_violated
        lifecycle.rollback_prepared = result.rollback_prepared
        lifecycle.rollback_gitops_status = (
            result.rollback_result.get("status") if result.rollback_result else None
        )
        lifecycle.rollback_branch = (
            result.rollback_result.get("branch") if result.rollback_result else None
        )
        lifecycle.rollback_pr_url = (
            result.rollback_result.get("pr_url") if result.rollback_result else None
        )
        lifecycle.rollback_commit_sha = (
            result.rollback_result.get("commit_sha") if result.rollback_result else None
        )
        return result

    # -----------------------------------------------------------------------
    # Stage 7: Final result
    # -----------------------------------------------------------------------

    @staticmethod
    def _stage_final_result(
        lifecycle: OptimizationLifecycle,
        *,
        validation: ValidatedRecommendation,
        verification: VerificationResult,
    ) -> None:
        """Seal the lifecycle with the top-level outcome."""
        outcome_map = {
            VerificationOutcome.SUCCESS: "SUCCESS",
            VerificationOutcome.DEGRADED: "DEGRADED",
            VerificationOutcome.ROLLBACK_REQUIRED: (
                "ROLLBACK_PREPARED" if verification.rollback_prepared else "ROLLBACK_FAILED"
            ),
            VerificationOutcome.INCONCLUSIVE: "INCONCLUSIVE",
        }
        final = outcome_map.get(verification.outcome, "UNKNOWN")

        lifecycle.complete(final)
        log.info(
            "controller.final_result",
            final_outcome=final,
            verification_outcome=verification.outcome,
            rollback_prepared=verification.rollback_prepared,
            lifecycle_id=lifecycle.lifecycle_id,
        )
