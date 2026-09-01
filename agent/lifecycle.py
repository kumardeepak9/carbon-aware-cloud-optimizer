"""
agent/lifecycle.py — Complete traceable lifecycle for a single GreenOps optimization.

Every optimization attempt has a unique lifecycle ID and advances through seven
ordered stages. Structured audit events are emitted at each transition so that
the full history — from carbon observation through Kubernetes deployment and
post-change verification — is permanently traceable.

Lifecycle stages (in order)
----------------------------
OBSERVATION      → Prometheus metrics collected; carbon + workload state captured.
RECOMMENDATION   → AI agent produced a DecisionRecommendation.
POLICY_VALIDATION→ OptimizationSafetyPolicy validated the recommendation.
GITOPS_CHANGE    → GitOpsChangeWorkflow prepared the branch + PR.
DEPLOYMENT       → Argo CD synced; Kubernetes reconciled to desired state.
VERIFICATION     → Post-change metrics collected and compared to pre-change baseline.
FINAL_RESULT     → Overall lifecycle outcome determined; rollback prepared if needed.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Stage enum
# ---------------------------------------------------------------------------


class LifecycleStage(StrEnum):
    """Ordered stages of a single GreenOps optimization cycle."""

    OBSERVATION = "OBSERVATION"
    RECOMMENDATION = "RECOMMENDATION"
    POLICY_VALIDATION = "POLICY_VALIDATION"
    GITOPS_CHANGE = "GITOPS_CHANGE"
    DEPLOYMENT = "DEPLOYMENT"
    VERIFICATION = "VERIFICATION"
    FINAL_RESULT = "FINAL_RESULT"


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """
    A single structured event emitted during a lifecycle stage transition.

    Every optimization emits one or more audit events per stage. Events are
    append-only — existing events are never mutated — so the complete history
    of decisions and intermediate states is always available.
    """

    lifecycle_id: str = Field(description="UUID of the parent OptimizationLifecycle.")
    stage: LifecycleStage = Field(description="Lifecycle stage that produced this event.")
    event_type: str = Field(
        description=(
            "Machine-readable event identifier, e.g. 'observation.collected', "
            "'gitops.pr_created', 'verification.rollback_prepared'."
        )
    )
    timestamp_seconds: float = Field(description="Unix epoch when the event was emitted.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured payload — depends on event_type.",
    )

    @classmethod
    def now(
        cls,
        *,
        lifecycle_id: str,
        stage: LifecycleStage,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Convenience constructor that stamps the current time."""
        return cls(
            lifecycle_id=lifecycle_id,
            stage=stage,
            event_type=event_type,
            timestamp_seconds=time.time(),
            data=data or {},
        )


# ---------------------------------------------------------------------------
# Lifecycle model
# ---------------------------------------------------------------------------


class OptimizationLifecycle(BaseModel):
    """
    Complete traceable record of a single GreenOps optimization attempt.

    Created at OBSERVATION and updated at each stage transition. The lifecycle
    is immutable once it reaches FINAL_RESULT — the controller creates a new
    lifecycle for the next optimization cycle.

    Field availability by stage
    ---------------------------
    OBSERVATION         : lifecycle_id, started_at, audit_events[0]
    RECOMMENDATION      : + recommendation_json
    POLICY_VALIDATION   : + validation_json
    GITOPS_CHANGE       : + gitops_status, gitops_branch, gitops_pr_url
    DEPLOYMENT          : + deployment_confirmed_at
    VERIFICATION        : + verification_outcome, pre_snapshot, post_snapshot, deltas
    FINAL_RESULT        : + final_outcome, completed_at, rollback_*
    """

    # Identity
    lifecycle_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = Field(default_factory=time.time)
    completed_at: float | None = None

    # Current stage
    current_stage: LifecycleStage = LifecycleStage.OBSERVATION

    # Stage outputs — populated as the lifecycle progresses
    # Stored as JSON-serialisable dicts so the lifecycle can be persisted
    # or logged without circular imports.
    recommendation_json: dict[str, Any] | None = None
    validation_json: dict[str, Any] | None = None

    gitops_status: str | None = None
    gitops_branch: str | None = None
    gitops_pr_url: str | None = None
    gitops_commit_sha: str | None = None

    deployment_confirmed_at: float | None = None

    verification_outcome: str | None = None
    verification_reason: str | None = None
    pre_snapshot_json: dict[str, Any] | None = None
    post_snapshot_json: dict[str, Any] | None = None
    metric_deltas_json: dict[str, Any] | None = None
    safety_thresholds_violated: list[str] = Field(default_factory=list)

    final_outcome: str | None = None

    rollback_prepared: bool = False
    rollback_gitops_status: str | None = None
    rollback_branch: str | None = None
    rollback_pr_url: str | None = None
    rollback_commit_sha: str | None = None

    # Append-only audit trail
    audit_events: list[AuditEvent] = Field(default_factory=list)

    # -----------------------------------------------------------------------
    # Audit helpers
    # -----------------------------------------------------------------------

    def emit(
        self,
        stage: LifecycleStage,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append a structured audit event to the lifecycle trail."""
        self.audit_events.append(
            AuditEvent.now(
                lifecycle_id=self.lifecycle_id,
                stage=stage,
                event_type=event_type,
                data=data or {},
            )
        )
        self.current_stage = stage

    def advance_to(self, stage: LifecycleStage) -> None:
        """Record a stage advancement without additional data."""
        self.emit(stage, f"{stage.value.lower()}.started")

    def complete(self, outcome: str) -> None:
        """Seal the lifecycle with a final outcome and timestamp."""
        self.final_outcome = outcome
        self.completed_at = time.time()
        self.emit(
            LifecycleStage.FINAL_RESULT,
            "lifecycle.completed",
            {
                "outcome": outcome,
                "duration_seconds": round(self.completed_at - self.started_at, 3),
            },
        )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a condensed dict suitable for structured logging."""
        return {
            "lifecycle_id": self.lifecycle_id,
            "current_stage": self.current_stage,
            "final_outcome": self.final_outcome,
            "gitops_status": self.gitops_status,
            "verification_outcome": self.verification_outcome,
            "rollback_prepared": self.rollback_prepared,
            "safety_violations": self.safety_thresholds_violated,
            "audit_event_count": len(self.audit_events),
            "duration_seconds": (
                round((self.completed_at or time.time()) - self.started_at, 3)
            ),
        }
