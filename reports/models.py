"""
reports/models.py — Typed report data models for the weekly GreenOps report.

Every numeric report value carries explicit provenance so the report can
distinguish direct measurements, deterministic calculations from measured
inputs, documented estimates, and unavailable values. This is a core design
constraint: the report must never present an estimate as if it were a
measurement.

Estimation methodology
----------------------
- ``estimated_cpu_hours_saved`` = Δreplicas × hours_at_reduced_scale × cpu_per_replica
- ``estimated_kwh_saved`` = estimated_cpu_hours_saved × watts_per_cpu_core / 1000
- ``estimated_co2_grams_avoided`` = estimated_kwh_saved × avg_carbon_intensity_gco2_kwh
- ``estimated_cost_saved_usd`` = estimated_cpu_hours_saved × hourly_rate_per_core

All estimation constants live in ``ReportEstimationConfig`` and are surfaced
in the report metadata so readers can verify the calculation chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Value provenance
# ---------------------------------------------------------------------------


class ValueProvenance(StrEnum):
    """How a report value was obtained."""

    MEASURED = "measured"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Value wrapper — measured vs. estimated
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportValue:
    """
    A single numeric value with provenance metadata.

    Fields
    ------
    value      : The numeric value itself (float or None if unavailable).
    measured   : Compatibility flag; True only for direct measurements.
    unit       : Human-readable unit string (e.g. 'gCO2eq/kWh', 'replicas', 'USD').
    note       : Optional free-text explanation of how the value was derived.
    provenance : measured, calculated, estimated, or unavailable.
    """

    value: float | None
    measured: bool
    unit: str = ""
    note: str = ""
    provenance: ValueProvenance | None = None

    def __post_init__(self) -> None:
        if self.provenance is None:
            provenance = (
                ValueProvenance.UNAVAILABLE
                if self.value is None
                else ValueProvenance.MEASURED
                if self.measured
                else ValueProvenance.ESTIMATED
            )
            object.__setattr__(self, "provenance", provenance)

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4) if self.value is not None else None,
            "measured": self.measured,
            "unit": self.unit,
            "note": self.note,
            "provenance": self.provenance.value if self.provenance else None,
        }


# ---------------------------------------------------------------------------
# Estimation configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportEstimationConfig:
    """
    Constants used to derive estimated values from measured data.

    These are surfaced in the report metadata so the reader can verify
    the calculation chain and adjust assumptions.
    """

    watts_per_cpu_core: float = 10.0
    """Average power draw per logical CPU core at typical load (watts)."""

    hourly_cost_per_cpu_core_usd: float = 0.035
    """On-demand cost per vCPU-hour — adjust per cloud provider / SKU."""

    default_cpu_per_replica_cores: float = 0.2
    """CPU request per replica (cores), used when actual requests are unknown."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "watts_per_cpu_core": self.watts_per_cpu_core,
            "hourly_cost_per_cpu_core_usd": self.hourly_cost_per_cpu_core_usd,
            "default_cpu_per_replica_cores": self.default_cpu_per_replica_cores,
        }


# ---------------------------------------------------------------------------
# Carbon trend summary
# ---------------------------------------------------------------------------


@dataclass
class CarbonTrendSummary:
    """Aggregated carbon intensity statistics for the reporting period."""

    avg_intensity_gco2_kwh: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "gCO2eq/kWh")
    )
    min_intensity_gco2_kwh: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "gCO2eq/kWh")
    )
    max_intensity_gco2_kwh: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "gCO2eq/kWh")
    )
    avg_renewable_pct: ReportValue = field(default_factory=lambda: ReportValue(None, False, "%"))
    avg_fossil_pct: ReportValue = field(default_factory=lambda: ReportValue(None, False, "%"))
    data_availability_pct: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "%")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_intensity_gco2_kwh": self.avg_intensity_gco2_kwh.to_dict(),
            "min_intensity_gco2_kwh": self.min_intensity_gco2_kwh.to_dict(),
            "max_intensity_gco2_kwh": self.max_intensity_gco2_kwh.to_dict(),
            "avg_renewable_pct": self.avg_renewable_pct.to_dict(),
            "avg_fossil_pct": self.avg_fossil_pct.to_dict(),
            "data_availability_pct": self.data_availability_pct.to_dict(),
        }


# ---------------------------------------------------------------------------
# Workload utilization summary
# ---------------------------------------------------------------------------


@dataclass
class WorkloadUtilizationSummary:
    """Averaged workload metrics for the reporting period."""

    avg_cpu_request_ratio: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "ratio")
    )
    avg_memory_request_ratio: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "ratio")
    )
    avg_replica_count: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "replicas")
    )
    avg_request_rate_rps: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "rps")
    )
    avg_p99_latency_seconds: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "seconds")
    )
    total_error_count: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "errors")
    )
    avg_availability_ratio: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "ratio")
    )

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k).to_dict() for k in self.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Optimization event record
# ---------------------------------------------------------------------------


class OptimizationOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    ROLLBACK_PREPARED = "ROLLBACK_PREPARED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    NO_ACTION = "NO_ACTION"
    READ_ONLY = "READ_ONLY"
    GITOPS_ERROR = "GITOPS_ERROR"
    ERROR = "ERROR"
    NO_OP = "NO_OP"


@dataclass
class OptimizationEventRecord:
    """
    One optimization lifecycle summarised for the report.

    Fields are populated from OptimizationLifecycle.
    """

    lifecycle_id: str = ""
    started_at: float = 0.0
    completed_at: float | None = None

    # Decision
    action: str = ""
    reason: str = ""
    decision_basis: str = ""
    confidence: float | None = None

    # Replicas
    pre_replicas: int | None = None
    post_replicas: int | None = None
    recommended_replicas: int | None = None

    # Policy
    policy_status: str = ""
    policy_reason: str = ""
    safeguards_triggered: list[str] = field(default_factory=list)

    # GitOps
    gitops_status: str | None = None
    gitops_branch: str | None = None
    gitops_pr_url: str | None = None

    # Verification
    verification_outcome: str | None = None
    verification_reason: str | None = None
    safety_violations: list[str] = field(default_factory=list)

    # Pre/post health
    pre_cpu_ratio: float | None = None
    post_cpu_ratio: float | None = None
    pre_memory_ratio: float | None = None
    post_memory_ratio: float | None = None
    pre_request_rate: float | None = None
    post_request_rate: float | None = None
    pre_p99_latency: float | None = None
    post_p99_latency: float | None = None
    pre_availability: float | None = None
    post_availability: float | None = None
    pre_error_rate: float | None = None
    post_error_rate: float | None = None

    # Rollback
    rollback_prepared: bool = False
    rollback_branch: str | None = None
    rollback_pr_url: str | None = None

    # Final
    final_outcome: str = ""

    @property
    def was_applied(self) -> bool:
        """True if the optimization reached the GitOps stage and was not blocked."""
        return self.gitops_status in {"PREPARED", "PR_CREATED"}

    @property
    def had_rollback(self) -> bool:
        return self.rollback_prepared

    @property
    def replica_delta(self) -> int | None:
        if self.pre_replicas is not None and self.post_replicas is not None:
            return self.post_replicas - self.pre_replicas
        return None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k in self.__dataclass_fields__:
            v = getattr(self, k)
            d[k] = v
        d["was_applied"] = self.was_applied
        d["had_rollback"] = self.had_rollback
        d["replica_delta"] = self.replica_delta
        return d


# ---------------------------------------------------------------------------
# Impact estimates
# ---------------------------------------------------------------------------


@dataclass
class ImpactEstimates:
    """
    Derived impact estimates.

    IMPORTANT: replica-hours are calculated from observed lifecycle state;
    downstream CPU, energy, carbon, and cost impacts are estimates based on
    ReportEstimationConfig. The report must clearly label each value.
    """

    total_replica_hours_saved: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "replica·hours")
    )
    estimated_cpu_hours_saved: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "CPU·hours")
    )
    estimated_kwh_saved: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "kWh")
    )
    estimated_co2_grams_avoided: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "gCO2eq")
    )
    estimated_cost_saved_usd: ReportValue = field(
        default_factory=lambda: ReportValue(None, False, "USD")
    )

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k).to_dict() for k in self.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Complete weekly report
# ---------------------------------------------------------------------------


@dataclass
class WeeklyGreenOpsReport:
    """
    The complete structured weekly report.

    Serialisable to JSON / dict for downstream rendering (Markdown, HTML, PDF).
    """

    # ── Metadata ──────────────────────────────────────────────────────────
    report_id: str = ""
    generated_at: str = ""
    period_start: str = ""
    period_end: str = ""
    region: str = ""
    deployment: str = ""
    namespace: str = ""

    estimation_config: dict[str, Any] = field(default_factory=dict)

    # ── Sections ──────────────────────────────────────────────────────────
    carbon_trends: CarbonTrendSummary = field(default_factory=CarbonTrendSummary)
    workload_utilization: WorkloadUtilizationSummary = field(
        default_factory=WorkloadUtilizationSummary
    )
    optimization_events: list[OptimizationEventRecord] = field(default_factory=list)
    impact_estimates: ImpactEstimates = field(default_factory=ImpactEstimates)

    # ── Aggregate counts ──────────────────────────────────────────────────
    total_optimization_cycles: int = 0
    total_applied: int = 0
    total_approved: int = 0
    total_rejected: int = 0
    total_deferred: int = 0
    total_rollbacks: int = 0
    total_errors: int = 0

    # ── Data quality ──────────────────────────────────────────────────────
    data_quality_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "region": self.region,
            "deployment": self.deployment,
            "namespace": self.namespace,
            "estimation_config": self.estimation_config,
            "carbon_trends": self.carbon_trends.to_dict(),
            "workload_utilization": self.workload_utilization.to_dict(),
            "optimization_events": [e.to_dict() for e in self.optimization_events],
            "impact_estimates": self.impact_estimates.to_dict(),
            "total_optimization_cycles": self.total_optimization_cycles,
            "total_applied": self.total_applied,
            "total_approved": self.total_approved,
            "total_rejected": self.total_rejected,
            "total_deferred": self.total_deferred,
            "total_rollbacks": self.total_rollbacks,
            "total_errors": self.total_errors,
            "data_quality_notes": self.data_quality_notes,
        }
