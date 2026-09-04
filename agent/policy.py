"""Deterministic, reliability-first policy for read-only recommendations."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from agent.models import (
    Action,
    DecisionMetadata,
    DecisionRecommendation,
    EnvironmentalContext,
    OperationalContext,
)
from monitoring.models import AgentObservation, MetricSnapshot


@dataclass(frozen=True)
class PolicyConfig:
    """Explicit, reviewable thresholds for the deterministic policy."""

    high_carbon_intensity: float = 250.0
    min_replicas: int = 1
    max_replicas: int = 10
    high_cpu_ratio: float = 0.80
    high_memory_ratio: float = 0.80
    high_request_rate_rps: float = 20.0
    low_request_rate_rps: float = 2.0
    high_error_rate_rps: float = 0.0
    high_p99_latency_seconds: float = 1.0
    max_carbon_data_age_seconds: float = 600.0


class DecisionPolicy:
    """Produces recommendations only; it contains no infrastructure clients or writers."""

    _REQUIRED_SIGNALS = frozenset(
        {
            "carbon_intensity_gco2_kwh",
            "carbon_data_available",
            "replica_count_desired",
            "replica_count_ready",
            "cpu_request_ratio",
            "memory_request_ratio",
            "http_request_rate_rps",
            "http_error_rate_rps",
            "http_p99_latency_seconds",
            "pod_restart_rate",
            "node_cpu_utilization_ratio",
        }
    )

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def recommend(
        self, observation: AgentObservation, now: float | None = None
    ) -> DecisionRecommendation:
        """Map a Prometheus observation to one safe, structured recommendation."""
        values, labels = self._values(observation.snapshots)
        environmental = EnvironmentalContext(
            carbon_intensity_gco2_kwh=values.get("carbon_intensity_gco2_kwh"),
            region=labels.get("carbon_intensity_gco2_kwh", {}).get("zone"),
            renewable_percentage=values.get("renewable_percentage"),
            fossil_fuel_percentage=values.get("fossil_fuel_percentage"),
            low_carbon_percentage=values.get("low_carbon_percentage"),
            data_available=self._as_bool(values.get("carbon_data_available")),
            data_timestamp_seconds=values.get("carbon_last_update_timestamp_seconds"),
        )
        operational = OperationalContext(
            current_replicas=self._as_replicas(values.get("replica_count_desired")),
            ready_replicas=self._as_replicas(values.get("replica_count_ready")),
            availability_ratio=values.get("pod_availability_ratio"),
            cpu_request_ratio=values.get("cpu_request_ratio"),
            memory_request_ratio=values.get("memory_request_ratio"),
            request_rate_rps=values.get("http_request_rate_rps"),
            error_rate_rps=values.get("http_error_rate_rps"),
            p99_latency_seconds=values.get("http_p99_latency_seconds"),
            restart_rate=values.get("pod_restart_rate"),
            node_cpu_utilization_ratio=values.get("node_cpu_utilization_ratio"),
            node_memory_available_bytes=values.get("node_memory_available_bytes"),
        )
        missing = sorted(self._REQUIRED_SIGNALS - values.keys())
        if environmental.data_available is not True:
            missing.append("carbon_data_available")
        current_time = time.time() if now is None else now
        if environmental.data_timestamp_seconds is not None and (
            current_time - environmental.data_timestamp_seconds
            > self.config.max_carbon_data_age_seconds
        ):
            missing.append("fresh_carbon_data")
        missing = sorted(set(missing))
        if missing:
            return self._result(
                Action.DEFER,
                operational,
                environmental,
                "Insufficient or stale monitoring data; no scaling recommendation is safe.",
                missing=missing,
                basis="missing_data",
            )

        guards = self._reliability_guards(operational)
        if guards:
            return self._result(
                Action.SCALE_UP,
                operational,
                environmental,
                "Application reliability guard triggered: " + ", ".join(guards) + ".",
                recommended=self._increase(operational.current_replicas),
                guards=guards,
                basis="reliability_priority",
            )

        if self._is_high_load(operational):
            return self._result(
                Action.SCALE_UP,
                operational,
                environmental,
                "Workload demand or resource pressure is high; reliability takes priority over carbon intensity.",
                recommended=self._increase(operational.current_replicas),
                basis="high_operational_load",
            )

        if (
            self._is_low_load(operational)
            and environmental.carbon_intensity_gco2_kwh is not None
            and environmental.carbon_intensity_gco2_kwh >= self.config.high_carbon_intensity
        ):
            return self._result(
                Action.SCALE_DOWN,
                operational,
                environmental,
                "Low workload demand during high grid carbon intensity; a one-replica reduction is safe to consider.",
                recommended=self._decrease(operational.current_replicas),
                basis="low_load_high_carbon",
            )

        return self._result(
            Action.KEEP,
            operational,
            environmental,
            "No reliability pressure or safe high-carbon, low-load reduction opportunity was detected.",
            recommended=operational.current_replicas,
            basis="steady_state",
        )

    @staticmethod
    def _values(
        snapshots: list[MetricSnapshot],
    ) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
        values: dict[str, float] = {}
        labels: dict[str, dict[str, str]] = {}
        for snapshot in snapshots:
            if math.isfinite(snapshot.value):
                values[snapshot.name] = snapshot.value
                labels[snapshot.name] = snapshot.labels
        return values, labels

    @staticmethod
    def _as_replicas(value: float | None) -> int | None:
        return None if value is None else max(0, round(value))

    @staticmethod
    def _as_bool(value: float | None) -> bool | None:
        return None if value is None else value >= 1.0

    def _reliability_guards(self, ctx: OperationalContext) -> list[str]:
        guards: list[str] = []
        if (
            ctx.ready_replicas is not None
            and ctx.current_replicas is not None
            and ctx.ready_replicas < ctx.current_replicas
        ):
            guards.append("not_all_replicas_ready")
        if ctx.availability_ratio is not None and ctx.availability_ratio < 1.0:
            guards.append("availability_below_target")
        if ctx.error_rate_rps is not None and ctx.error_rate_rps > self.config.high_error_rate_rps:
            guards.append("http_errors")
        if (
            ctx.p99_latency_seconds is not None
            and ctx.p99_latency_seconds > self.config.high_p99_latency_seconds
        ):
            guards.append("high_p99_latency")
        if ctx.restart_rate is not None and ctx.restart_rate > 0:
            guards.append("pod_restarts")
        return guards

    def _is_high_load(self, ctx: OperationalContext) -> bool:
        return any(
            (
                (ctx.cpu_request_ratio or 0.0) >= self.config.high_cpu_ratio,
                (ctx.memory_request_ratio or 0.0) >= self.config.high_memory_ratio,
                (ctx.request_rate_rps or 0.0) >= self.config.high_request_rate_rps,
            )
        )

    def _is_low_load(self, ctx: OperationalContext) -> bool:
        return (
            (ctx.cpu_request_ratio or 0.0) < self.config.high_cpu_ratio
            and (ctx.memory_request_ratio or 0.0) < self.config.high_memory_ratio
            and (ctx.request_rate_rps or 0.0) <= self.config.low_request_rate_rps
        )

    def _increase(self, current: int | None) -> int | None:
        return None if current is None else min(current + 1, self.config.max_replicas)

    def _decrease(self, current: int | None) -> int | None:
        return None if current is None else max(current - 1, self.config.min_replicas)

    def _result(
        self,
        action: Action,
        operational: OperationalContext,
        environmental: EnvironmentalContext,
        reason: str,
        *,
        recommended: int | None = None,
        missing: list[str] | None = None,
        guards: list[str] | None = None,
        basis: str,
    ) -> DecisionRecommendation:
        return DecisionRecommendation(
            action=action,
            current_replicas=operational.current_replicas,
            recommended_replicas=recommended,
            reason=reason,
            environmental_context=environmental,
            operational_context=operational,
            metadata=DecisionMetadata(
                confidence=0.0 if missing else 0.95,
                missing_signals=missing or [],
                safety_guards_triggered=guards or [],
                decision_basis=basis,
            ),
        )
