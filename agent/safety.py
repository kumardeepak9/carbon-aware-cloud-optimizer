"""Deterministic Phase 7 safety validation for optimization recommendations."""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent.models import (
    Action,
    DecisionRecommendation,
    OperationalContext,
    PolicyValidation,
    ValidationStatus,
)


@dataclass(frozen=True)
class OptimizationSafetyConfig:
    """Configurable guardrails checked before any future infrastructure action."""

    min_replicas: int = 1
    max_replicas: int = 10
    cpu_safety_threshold: float = 0.70
    latency_sla_threshold_seconds: float = 1.0
    require_all_replicas_ready: bool = True
    reject_on_http_errors: bool = True
    reject_on_restarts: bool = True
    max_scale_down_percentage: float = 0.50
    cooldown_seconds: float = 900.0
    max_carbon_data_age_seconds: float = 600.0


class OptimizationSafetyPolicy:
    """Validates AI recommendations before they can become GitOps changes."""

    def __init__(self, config: OptimizationSafetyConfig | None = None) -> None:
        self.config = config or OptimizationSafetyConfig()

    def validate(
        self,
        recommendation: DecisionRecommendation,
        *,
        now: float | None = None,
        last_optimization_timestamp_seconds: float | None = None,
    ) -> PolicyValidation:
        """Return a deterministic verdict with a human-readable reason."""
        evaluated_at = time.time() if now is None else now
        blockers = self._hard_rejections(recommendation, evaluated_at)
        if blockers:
            return self._verdict(ValidationStatus.REJECTED, blockers, evaluated_at)

        review = self._review_conditions(
            recommendation, evaluated_at, last_optimization_timestamp_seconds
        )
        if review:
            return self._verdict(ValidationStatus.REQUIRE_REVIEW, review, evaluated_at)

        return PolicyValidation(
            status=ValidationStatus.APPROVED,
            reason="Recommendation satisfies all deterministic safety safeguards.",
            approved_for_gitops_change=recommendation.action in {Action.SCALE_DOWN, Action.SCALE_UP},
            safeguards_triggered=[],
            evaluated_at_seconds=evaluated_at,
        )

    def _hard_rejections(
        self, recommendation: DecisionRecommendation, evaluated_at: float
    ) -> list[str]:
        reasons: list[str] = []
        ctx = recommendation.operational_context
        env = recommendation.environmental_context

        if recommendation.metadata.missing_signals:
            reasons.append(
                "missing required metric signals: "
                + ", ".join(sorted(recommendation.metadata.missing_signals))
            )
        if env.data_available is not True:
            reasons.append("carbon data is unavailable")
        if env.data_timestamp_seconds is None:
            reasons.append("carbon data timestamp is missing")
        elif evaluated_at - env.data_timestamp_seconds > self.config.max_carbon_data_age_seconds:
            reasons.append("carbon data is stale")

        if recommendation.action is Action.DEFER:
            reasons.append("agent deferred because current data is not safe for optimization")

        if recommendation.action in {Action.SCALE_DOWN, Action.SCALE_UP}:
            if recommendation.current_replicas is None:
                reasons.append("current replica count is missing")
            if recommendation.recommended_replicas is None:
                reasons.append("recommended replica count is missing")

        target = recommendation.recommended_replicas
        if target is not None:
            if target < self.config.min_replicas:
                reasons.append(
                    f"recommended replicas {target} is below minimum {self.config.min_replicas}"
                )
            if target > self.config.max_replicas:
                reasons.append(
                    f"recommended replicas {target} exceeds maximum {self.config.max_replicas}"
                )

        if recommendation.action is Action.SCALE_DOWN:
            if (
                ctx.cpu_request_ratio is not None
                and ctx.cpu_request_ratio >= self.config.cpu_safety_threshold
            ):
                reasons.append("CPU utilization is above the scale-down safety threshold")
            if (
                ctx.p99_latency_seconds is not None
                and ctx.p99_latency_seconds >= self.config.latency_sla_threshold_seconds
            ):
                reasons.append("P99 latency is at or above the SLA threshold")
            if self.config.require_all_replicas_ready and not self._is_healthy(ctx):
                reasons.append("application health requirements are not satisfied")
            if self.config.reject_on_http_errors and (ctx.error_rate_rps or 0.0) > 0.0:
                reasons.append("HTTP errors are present")
            if self.config.reject_on_restarts and (ctx.restart_rate or 0.0) > 0.0:
                reasons.append("pod restarts are present")

        return reasons

    def _review_conditions(
        self,
        recommendation: DecisionRecommendation,
        evaluated_at: float,
        last_optimization_timestamp_seconds: float | None,
    ) -> list[str]:
        reasons: list[str] = []
        ctx = recommendation.operational_context

        if recommendation.action in {Action.SCALE_DOWN, Action.SCALE_UP}:
            if last_optimization_timestamp_seconds is not None:
                elapsed = evaluated_at - last_optimization_timestamp_seconds
                if elapsed < self.config.cooldown_seconds:
                    reasons.append(
                        "cooldown period has not elapsed "
                        f"({elapsed:.0f}s < {self.config.cooldown_seconds:.0f}s)"
                    )

        if recommendation.action is Action.SCALE_DOWN:
            current = recommendation.current_replicas
            target = recommendation.recommended_replicas
            if current and target is not None and target < current:
                reduction = (current - target) / current
                if reduction > self.config.max_scale_down_percentage:
                    reasons.append(
                        "scale-down reduction exceeds configured percentage "
                        f"({reduction:.0%} > {self.config.max_scale_down_percentage:.0%})"
                    )

        if recommendation.action is Action.KEEP and not self._is_healthy(ctx):
            reasons.append("agent recommended KEEP while application health is degraded")

        if (
            recommendation.action is Action.SCALE_UP
            and recommendation.current_replicas == self.config.max_replicas
            and recommendation.recommended_replicas == self.config.max_replicas
        ):
            reasons.append("scale-up is requested but the workload is already at maximum replicas")

        return reasons

    @staticmethod
    def _is_healthy(ctx: OperationalContext) -> bool:
        ready = ctx.ready_replicas
        current = ctx.current_replicas
        availability = ctx.availability_ratio
        errors = ctx.error_rate_rps
        restarts = ctx.restart_rate
        if ready is not None and current is not None and ready < current:
            return False
        if availability is not None and availability < 1.0:
            return False
        if errors is not None and errors > 0.0:
            return False
        return not (restarts is not None and restarts > 0.0)

    @staticmethod
    def _verdict(
        status: ValidationStatus,
        reasons: list[str],
        evaluated_at: float,
    ) -> PolicyValidation:
        return PolicyValidation(
            status=status,
            reason="; ".join(reasons) + ".",
            approved_for_gitops_change=False,
            safeguards_triggered=reasons,
            evaluated_at_seconds=evaluated_at,
        )
