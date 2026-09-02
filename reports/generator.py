"""
reports/generator.py — Weekly GreenOps report generator.

Consumes:
  - A list of OptimizationLifecycle records from the reporting period.
  - Optional Prometheus time-series summaries (carbon trends, workload averages).

Produces:
  - A WeeklyGreenOpsReport with every value tagged as measured or estimated.

The generator never fabricates values. If data is unavailable:
  - ReportValue.value is set to None.
  - A data_quality_note is appended explaining what is missing.

Estimation chain (only when measured inputs are available)
----------------------------------------------------------
1. replica_hours_saved  = Σ (original_replicas - reduced_replicas) × duration_hours
                          [calculated: replica counts and duration from lifecycle snapshots]
2. cpu_hours_saved      = replica_hours_saved × cpu_per_replica
                          [estimated: uses ReportEstimationConfig.default_cpu_per_replica_cores]
3. kwh_saved            = cpu_hours_saved × watts_per_core / 1000
                          [estimated: uses ReportEstimationConfig.watts_per_cpu_core]
4. co2_grams_avoided    = kwh_saved × avg_carbon_intensity
                          [estimated: carbon intensity is measured, product is derived]
5. cost_saved_usd       = cpu_hours_saved × hourly_rate
                          [estimated: uses ReportEstimationConfig.hourly_cost_per_cpu_core_usd]
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from agent.lifecycle import OptimizationLifecycle
from reports.models import (
    CarbonTrendSummary,
    ImpactEstimates,
    OptimizationEventRecord,
    ReportEstimationConfig,
    ReportValue,
    ValueProvenance,
    WeeklyGreenOpsReport,
    WorkloadUtilizationSummary,
)


class WeeklyReportGenerator:
    """
    Builds a WeeklyGreenOpsReport from lifecycle records and optional metric summaries.

    Usage::

        generator = WeeklyReportGenerator(
            lifecycles=completed_cycles,
            carbon_summary=prom_carbon_data,      # optional
            workload_summary=prom_workload_data,   # optional
        )
        report = generator.generate(
            period_start=start_dt,
            period_end=end_dt,
        )
    """

    def __init__(
        self,
        *,
        lifecycles: list[OptimizationLifecycle] | None = None,
        carbon_summary: dict[str, float | None] | None = None,
        workload_summary: dict[str, float | None] | None = None,
        estimation_config: ReportEstimationConfig | None = None,
        region: str = "",
        namespace: str = "greenops",
        deployment: str = "greenops-demo-workload",
    ) -> None:
        self._lifecycles = lifecycles or []
        self._carbon = carbon_summary or {}
        self._workload = workload_summary or {}
        self._config = estimation_config or ReportEstimationConfig()
        self._region = region
        self._namespace = namespace
        self._deployment = deployment

    def generate(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> WeeklyGreenOpsReport:
        """Generate the complete weekly report."""
        notes: list[str] = []

        events = [self._lifecycle_to_event(lc) for lc in self._lifecycles]
        carbon = self._build_carbon_trends(notes)
        workload = self._build_workload_utilization(notes)
        counts = self._compute_counts(events)
        impact = self._compute_impact_estimates(events, carbon, notes)

        report = WeeklyGreenOpsReport(
            report_id=str(uuid.uuid4()),
            generated_at=datetime.now(UTC).isoformat(),
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            region=self._region,
            deployment=self._deployment,
            namespace=self._namespace,
            estimation_config=self._config.to_dict(),
            carbon_trends=carbon,
            workload_utilization=workload,
            optimization_events=events,
            impact_estimates=impact,
            data_quality_notes=notes,
            **counts,
        )
        return report

    # -----------------------------------------------------------------------
    # Lifecycle → event record
    # -----------------------------------------------------------------------

    @staticmethod
    def _lifecycle_to_event(lc: OptimizationLifecycle) -> OptimizationEventRecord:
        """Convert one lifecycle into a flat report record."""
        rec = lc.recommendation_json or {}
        val = lc.validation_json or {}
        metadata = rec.get("metadata", {})
        pre = lc.pre_snapshot_json or {}
        post = lc.post_snapshot_json or {}

        return OptimizationEventRecord(
            lifecycle_id=lc.lifecycle_id,
            started_at=lc.started_at,
            completed_at=lc.completed_at,
            action=rec.get("action", ""),
            reason=rec.get("reason", ""),
            decision_basis=metadata.get("decision_basis", ""),
            confidence=metadata.get("confidence"),
            pre_replicas=_first_int(
                pre.get("replica_count_desired"),
                rec.get("current_replicas"),
            ),
            post_replicas=_int_or_none(post.get("replica_count_desired")),
            recommended_replicas=_int_or_none(rec.get("recommended_replicas")),
            policy_status=val.get("status", ""),
            policy_reason=val.get("reason", ""),
            safeguards_triggered=val.get("safeguards_triggered", []),
            gitops_status=lc.gitops_status,
            gitops_branch=lc.gitops_branch,
            gitops_pr_url=lc.gitops_pr_url,
            verification_outcome=lc.verification_outcome,
            verification_reason=lc.verification_reason,
            safety_violations=lc.safety_thresholds_violated,
            pre_cpu_ratio=pre.get("cpu_request_ratio"),
            post_cpu_ratio=post.get("cpu_request_ratio"),
            pre_memory_ratio=pre.get("memory_request_ratio"),
            post_memory_ratio=post.get("memory_request_ratio"),
            pre_request_rate=pre.get("http_request_rate_rps"),
            post_request_rate=post.get("http_request_rate_rps"),
            pre_p99_latency=pre.get("http_p99_latency_seconds"),
            post_p99_latency=post.get("http_p99_latency_seconds"),
            pre_availability=pre.get("availability_ratio"),
            post_availability=post.get("availability_ratio"),
            pre_error_rate=pre.get("http_error_rate_rps"),
            post_error_rate=post.get("http_error_rate_rps"),
            rollback_prepared=lc.rollback_prepared,
            rollback_branch=lc.rollback_branch,
            rollback_pr_url=lc.rollback_pr_url,
            final_outcome=lc.final_outcome or "",
        )

    # -----------------------------------------------------------------------
    # Carbon trends
    # -----------------------------------------------------------------------

    def _build_carbon_trends(self, notes: list[str]) -> CarbonTrendSummary:
        c = self._carbon
        has_data = any(c.get(k) is not None for k in [
            "avg_intensity", "min_intensity", "max_intensity",
        ])
        required = {
            "avg_intensity": "average carbon intensity",
            "min_intensity": "minimum carbon intensity",
            "max_intensity": "maximum carbon intensity",
            "avg_renewable_pct": "average renewable percentage",
            "avg_fossil_pct": "average fossil percentage",
            "data_availability_pct": "carbon data availability percentage",
        }
        missing = [label for key, label in required.items() if c.get(key) is None]
        if missing:
            notes.append(
                "Carbon intensity trend data missing: "
                + ", ".join(missing)
                + ". Missing values are reported as unavailable."
            )
        if not has_data:
            notes.append("Carbon trend section contains no measured values.")

        return CarbonTrendSummary(
            avg_intensity_gco2_kwh=ReportValue(
                c.get("avg_intensity"), measured=c.get("avg_intensity") is not None,
                unit="gCO2eq/kWh",
            ),
            min_intensity_gco2_kwh=ReportValue(
                c.get("min_intensity"), measured=c.get("min_intensity") is not None,
                unit="gCO2eq/kWh",
            ),
            max_intensity_gco2_kwh=ReportValue(
                c.get("max_intensity"), measured=c.get("max_intensity") is not None,
                unit="gCO2eq/kWh",
            ),
            avg_renewable_pct=ReportValue(
                c.get("avg_renewable_pct"), measured=c.get("avg_renewable_pct") is not None,
                unit="%",
            ),
            avg_fossil_pct=ReportValue(
                c.get("avg_fossil_pct"), measured=c.get("avg_fossil_pct") is not None,
                unit="%",
            ),
            data_availability_pct=ReportValue(
                c.get("data_availability_pct"),
                measured=c.get("data_availability_pct") is not None,
                unit="%",
            ),
        )

    # -----------------------------------------------------------------------
    # Workload utilization
    # -----------------------------------------------------------------------

    def _build_workload_utilization(self, notes: list[str]) -> WorkloadUtilizationSummary:
        w = self._workload
        has_data = any(w.get(k) is not None for k in [
            "avg_cpu_ratio", "avg_memory_ratio", "avg_replicas",
        ])
        required = {
            "avg_cpu_ratio": "average CPU request ratio",
            "avg_memory_ratio": "average memory request ratio",
            "avg_replicas": "average replica count",
            "avg_request_rate": "average request rate",
            "avg_p99_latency": "average P99 latency",
            "total_errors": "total HTTP errors",
            "avg_availability": "average availability ratio",
        }
        missing = [label for key, label in required.items() if w.get(key) is None]
        if missing:
            notes.append(
                "Workload utilization data missing: "
                + ", ".join(missing)
                + ". Missing values are reported as unavailable."
            )
        if not has_data:
            notes.append("Utilization section contains no measured values.")

        return WorkloadUtilizationSummary(
            avg_cpu_request_ratio=ReportValue(
                w.get("avg_cpu_ratio"), measured=w.get("avg_cpu_ratio") is not None,
                unit="ratio",
            ),
            avg_memory_request_ratio=ReportValue(
                w.get("avg_memory_ratio"), measured=w.get("avg_memory_ratio") is not None,
                unit="ratio",
            ),
            avg_replica_count=ReportValue(
                w.get("avg_replicas"), measured=w.get("avg_replicas") is not None,
                unit="replicas",
            ),
            avg_request_rate_rps=ReportValue(
                w.get("avg_request_rate"), measured=w.get("avg_request_rate") is not None,
                unit="rps",
            ),
            avg_p99_latency_seconds=ReportValue(
                w.get("avg_p99_latency"), measured=w.get("avg_p99_latency") is not None,
                unit="seconds",
            ),
            total_error_count=ReportValue(
                w.get("total_errors"), measured=w.get("total_errors") is not None,
                unit="errors",
            ),
            avg_availability_ratio=ReportValue(
                w.get("avg_availability"), measured=w.get("avg_availability") is not None,
                unit="ratio",
            ),
        )

    # -----------------------------------------------------------------------
    # Aggregate counts
    # -----------------------------------------------------------------------

    @staticmethod
    def _compute_counts(events: list[OptimizationEventRecord]) -> dict[str, int]:
        total = len(events)
        applied = sum(1 for e in events if e.was_applied)
        approved = sum(1 for e in events if e.policy_status == "APPROVED")
        rejected = sum(1 for e in events if e.policy_status == "REJECTED")
        deferred = sum(1 for e in events if e.final_outcome == "DEFERRED")
        rollbacks = sum(1 for e in events if e.had_rollback)
        errors = sum(1 for e in events if e.final_outcome in {"ERROR", "GITOPS_ERROR"})

        return {
            "total_optimization_cycles": total,
            "total_applied": applied,
            "total_approved": approved,
            "total_rejected": rejected,
            "total_deferred": deferred,
            "total_rollbacks": rollbacks,
            "total_errors": errors,
        }

    # -----------------------------------------------------------------------
    # Impact estimation
    # -----------------------------------------------------------------------

    def _compute_impact_estimates(
        self,
        events: list[OptimizationEventRecord],
        carbon: CarbonTrendSummary,
        notes: list[str],
    ) -> ImpactEstimates:
        """
        Derive impact estimates from measured lifecycle data.

        The estimation chain is:
          replica_hours → cpu_hours → kWh → CO2 → cost

        Each step only proceeds if its inputs are available. If any link
        in the chain is missing, downstream values are left as None and
        a data quality note is appended.
        """
        cfg = self._config

        # Step 1: replica·hours saved (calculated from lifecycle state)
        total_replica_hours = 0.0
        has_replica_data = False

        for event in events:
            if not event.was_applied or event.final_outcome != "SUCCESS" or event.had_rollback:
                continue
            delta = event.replica_delta
            if delta is None or delta >= 0:
                # Only count scale-downs as savings
                continue
            if event.started_at and event.completed_at:
                duration_hours = (event.completed_at - event.started_at) / 3600.0
            else:
                continue
            saved = abs(delta) * duration_hours
            total_replica_hours += saved
            has_replica_data = True

        if not has_replica_data:
            notes.append(
                "No verified successful scale-down events with observed pre/post "
                "replica counts and durations were found. Impact estimates are unavailable."
            )
            return ImpactEstimates()

        replica_hours_val = ReportValue(
            round(total_replica_hours, 4),
            measured=False,
            unit="replica·hours",
            note=(
                "Calculated as Σ(abs(post_replicas - pre_replicas) × "
                "lifecycle_duration_hours) for verified successful scale-down events."
            ),
            provenance=ValueProvenance.CALCULATED,
        )

        # Step 2: estimated CPU·hours saved
        cpu_hours = total_replica_hours * cfg.default_cpu_per_replica_cores
        cpu_hours_val = ReportValue(
            round(cpu_hours, 4),
            measured=False,
            unit="CPU·hours",
            note=f"replica_hours × {cfg.default_cpu_per_replica_cores} cores/replica (estimated).",
            provenance=ValueProvenance.ESTIMATED,
        )

        # Step 3: estimated kWh saved
        kwh = cpu_hours * cfg.watts_per_cpu_core / 1000.0
        kwh_val = ReportValue(
            round(kwh, 6),
            measured=False,
            unit="kWh",
            note=f"cpu_hours × {cfg.watts_per_cpu_core}W / 1000 (estimated).",
            provenance=ValueProvenance.ESTIMATED,
        )

        # Step 4: estimated CO2 grams avoided (requires measured carbon intensity)
        avg_carbon = carbon.avg_intensity_gco2_kwh.value
        if avg_carbon is not None:
            co2 = kwh * avg_carbon
            co2_val = ReportValue(
                round(co2, 2),
                measured=False,
                unit="gCO2eq",
                note=(
                    f"kwh_saved × {avg_carbon:.1f} gCO2eq/kWh (avg measured intensity). "
                    "The intensity is measured; the product is estimated."
                ),
                provenance=ValueProvenance.ESTIMATED,
            )
        else:
            co2_val = ReportValue(
                None,
                False,
                "gCO2eq",
                note="Unavailable: avg carbon intensity not provided.",
                provenance=ValueProvenance.UNAVAILABLE,
            )
            notes.append(
                "Estimated CO2 avoided is unavailable because average carbon "
                "intensity was not provided for the reporting period."
            )

        # Step 5: estimated cost savings
        cost = cpu_hours * cfg.hourly_cost_per_cpu_core_usd
        cost_val = ReportValue(
            round(cost, 4),
            measured=False,
            unit="USD",
            note=(
                f"cpu_hours × ${cfg.hourly_cost_per_cpu_core_usd}/core·hour. "
                "Based on generic on-demand pricing; actual savings depend on "
                "pricing model, reserved instances, and spot usage."
            ),
            provenance=ValueProvenance.ESTIMATED,
        )

        return ImpactEstimates(
            total_replica_hours_saved=replica_hours_val,
            estimated_cpu_hours_saved=cpu_hours_val,
            estimated_kwh_saved=kwh_val,
            estimated_co2_grams_avoided=co2_val,
            estimated_cost_saved_usd=cost_val,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _int_or_none(v: Any) -> int | None:
    """Safely extract an int from a value that might be float, str, or None."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _first_int(*values: Any) -> int | None:
    """Return the first value that can be safely converted to int."""
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None
