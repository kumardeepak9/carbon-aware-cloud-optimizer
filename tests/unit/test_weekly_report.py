"""
tests/unit/test_weekly_report.py

Tests for the Phase 11 weekly GreenOps reporting module.

Test classes
------------
TestReportValue                    : measured/estimated provenance.
TestReportEstimationConfig         : default constants and serialisation.
TestOptimizationEventRecord        : lifecycle-to-event conversion helpers.
TestWeeklyReportGeneratorComplete  : full data — all sections populated.
TestWeeklyReportGeneratorPartial   : partial data — graceful degradation.
TestWeeklyReportGeneratorEmpty     : no data — no fabricated values.
TestImpactEstimationChain          : estimation chain step-by-step.
TestMarkdownRenderer               : Markdown rendering + provenance markers.
TestDataQualityNotes               : notes appended for missing data.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import pytest

from agent.lifecycle import LifecycleStage, OptimizationLifecycle
from reports.generator import WeeklyReportGenerator
from reports.models import (
    OptimizationEventRecord,
    ReportEstimationConfig,
    ReportValue,
    ValueProvenance,
)
from reports.renderer import render_markdown

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

PERIOD_START = datetime(2026, 8, 18, 0, 0, 0, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)


def make_lifecycle(
    *,
    action: str = "SCALE_DOWN",
    current_replicas: int = 3,
    recommended_replicas: int = 1,
    final_outcome: str = "SUCCESS",
    policy_status: str = "APPROVED",
    gitops_status: str = "PR_CREATED",
    verification_outcome: str = "SUCCESS",
    duration_hours: float = 4.0,
    carbon_intensity: float = 310.0,
    pre_cpu: float = 0.40,
    post_cpu: float = 0.55,
    pre_memory: float = 0.45,
    post_memory: float = 0.60,
    pre_request_rate: float = 1.0,
    post_request_rate: float = 0.9,
    pre_p99: float = 0.12,
    post_p99: float = 0.18,
    rollback: bool = False,
) -> OptimizationLifecycle:
    """Build a realistic lifecycle with populated stage outputs."""
    now = time.time()
    lc = OptimizationLifecycle(
        lifecycle_id=str(uuid.uuid4()),
        started_at=now - duration_hours * 3600,
        completed_at=now,
    )
    lc.recommendation_json = {
        "action": action,
        "current_replicas": current_replicas,
        "recommended_replicas": recommended_replicas,
        "reason": f"Carbon at {carbon_intensity} gCO2/kWh; safe to reduce.",
        "metadata": {
            "decision_basis": "high-carbon-low-load",
            "confidence": 0.85,
            "missing_signals": [],
        },
        "environmental_context": {
            "carbon_intensity_gco2_kwh": carbon_intensity,
            "data_available": True,
        },
    }
    lc.validation_json = {
        "status": policy_status,
        "reason": "All safety checks passed.",
        "approved_for_gitops_change": policy_status == "APPROVED",
        "safeguards_triggered": [],
    }
    lc.gitops_status = gitops_status
    lc.gitops_branch = "greenops/scale-down-to-1"
    lc.gitops_pr_url = "https://github.com/test/pr/42"
    lc.gitops_commit_sha = "abc123"
    lc.verification_outcome = verification_outcome
    lc.verification_reason = "All thresholds satisfied."
    lc.pre_snapshot_json = {
        "cpu_request_ratio": pre_cpu,
        "memory_request_ratio": pre_memory,
        "http_request_rate_rps": pre_request_rate,
        "http_p99_latency_seconds": pre_p99,
        "availability_ratio": 1.0,
        "replica_count_desired": float(current_replicas),
        "http_error_rate_rps": 0.0,
    }
    lc.post_snapshot_json = {
        "cpu_request_ratio": post_cpu,
        "memory_request_ratio": post_memory,
        "http_request_rate_rps": post_request_rate,
        "http_p99_latency_seconds": post_p99,
        "availability_ratio": 1.0,
        "replica_count_desired": float(recommended_replicas),
        "http_error_rate_rps": 0.0,
    }
    lc.final_outcome = final_outcome
    lc.rollback_prepared = rollback
    lc.rollback_branch = "greenops/rollback" if rollback else None
    lc.rollback_pr_url = "https://github.com/test/pr/99" if rollback else None
    lc.safety_thresholds_violated = ["P99 latency 1.5s > SLA 1.0s"] if rollback else []

    # Emit some audit events
    lc.emit(LifecycleStage.OBSERVATION, "observation.collected", {})
    lc.emit(LifecycleStage.FINAL_RESULT, "lifecycle.completed", {"outcome": final_outcome})

    return lc


@pytest.fixture()
def full_carbon_summary() -> dict[str, float]:
    return {
        "avg_intensity": 215.0,
        "min_intensity": 85.0,
        "max_intensity": 420.0,
        "avg_renewable_pct": 42.0,
        "avg_fossil_pct": 38.0,
        "data_availability_pct": 98.5,
    }


@pytest.fixture()
def full_workload_summary() -> dict[str, float]:
    return {
        "avg_cpu_ratio": 0.45,
        "avg_memory_ratio": 0.55,
        "avg_replicas": 2.3,
        "avg_request_rate": 12.5,
        "avg_p99_latency": 0.15,
        "total_errors": 3.0,
        "avg_availability": 0.998,
    }


# ===========================================================================
# TestReportValue
# ===========================================================================


class TestReportValue:
    def test_measured_value(self):
        rv = ReportValue(42.0, measured=True, unit="gCO2eq/kWh")
        assert rv.available is True
        assert rv.measured is True
        assert rv.unit == "gCO2eq/kWh"

    def test_estimated_value(self):
        rv = ReportValue(1.5, measured=False, unit="kWh", note="derived")
        assert rv.measured is False
        assert rv.provenance is ValueProvenance.ESTIMATED
        assert rv.note == "derived"

    def test_calculated_value(self):
        rv = ReportValue(
            8.0,
            measured=False,
            unit="replica·hours",
            provenance=ValueProvenance.CALCULATED,
        )
        assert rv.measured is False
        assert rv.provenance is ValueProvenance.CALCULATED

    def test_unavailable_value(self):
        rv = ReportValue(None, measured=False)
        assert rv.available is False
        assert rv.provenance is ValueProvenance.UNAVAILABLE

    def test_to_dict(self):
        rv = ReportValue(3.14159, measured=True, unit="ratio")
        d = rv.to_dict()
        assert d["value"] == 3.1416  # rounded to 4 dp
        assert d["measured"] is True
        assert d["unit"] == "ratio"
        assert d["provenance"] == "measured"

    def test_to_dict_none(self):
        rv = ReportValue(None, measured=False)
        d = rv.to_dict()
        assert d["value"] is None
        assert d["measured"] is False


# ===========================================================================
# TestReportEstimationConfig
# ===========================================================================


class TestReportEstimationConfig:
    def test_defaults(self):
        cfg = ReportEstimationConfig()
        assert cfg.watts_per_cpu_core == 10.0
        assert cfg.hourly_cost_per_cpu_core_usd == 0.035
        assert cfg.default_cpu_per_replica_cores == 0.2

    def test_custom_values(self):
        cfg = ReportEstimationConfig(watts_per_cpu_core=15.0, hourly_cost_per_cpu_core_usd=0.05)
        assert cfg.watts_per_cpu_core == 15.0

    def test_to_dict(self):
        cfg = ReportEstimationConfig()
        d = cfg.to_dict()
        assert "watts_per_cpu_core" in d
        assert "hourly_cost_per_cpu_core_usd" in d


# ===========================================================================
# TestOptimizationEventRecord
# ===========================================================================


class TestOptimizationEventRecord:
    def test_was_applied_pr_created(self):
        ev = OptimizationEventRecord(gitops_status="PR_CREATED")
        assert ev.was_applied is True

    def test_was_applied_prepared(self):
        ev = OptimizationEventRecord(gitops_status="PREPARED")
        assert ev.was_applied is True

    def test_was_applied_blocked(self):
        ev = OptimizationEventRecord(gitops_status="BLOCKED")
        assert ev.was_applied is False

    def test_was_applied_none(self):
        ev = OptimizationEventRecord(gitops_status=None)
        assert ev.was_applied is False

    def test_replica_delta(self):
        ev = OptimizationEventRecord(pre_replicas=3, post_replicas=1)
        assert ev.replica_delta == -2

    def test_replica_delta_none(self):
        ev = OptimizationEventRecord(pre_replicas=3, post_replicas=None)
        assert ev.replica_delta is None

    def test_had_rollback(self):
        ev = OptimizationEventRecord(rollback_prepared=True)
        assert ev.had_rollback is True

    def test_to_dict_includes_computed(self):
        ev = OptimizationEventRecord(pre_replicas=3, post_replicas=1, gitops_status="PR_CREATED")
        d = ev.to_dict()
        assert d["was_applied"] is True
        assert d["replica_delta"] == -2


# ===========================================================================
# TestWeeklyReportGeneratorComplete
# ===========================================================================


class TestWeeklyReportGeneratorComplete:
    """Full data — all sections should be populated with measured values."""

    def test_complete_report_structure(self, full_carbon_summary, full_workload_summary):
        lc1 = make_lifecycle(final_outcome="SUCCESS", duration_hours=4.0)
        lc2 = make_lifecycle(
            action="SCALE_DOWN",
            current_replicas=4,
            recommended_replicas=2,
            final_outcome="DEGRADED",
            verification_outcome="DEGRADED",
            duration_hours=2.0,
        )

        gen = WeeklyReportGenerator(
            lifecycles=[lc1, lc2],
            carbon_summary=full_carbon_summary,
            workload_summary=full_workload_summary,
            region="DE",
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.report_id
        assert report.period_start == PERIOD_START.isoformat()
        assert report.period_end == PERIOD_END.isoformat()
        assert report.region == "DE"
        assert report.total_optimization_cycles == 2
        assert report.total_applied == 2

    def test_carbon_trends_measured(self, full_carbon_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.carbon_trends.avg_intensity_gco2_kwh.measured is True
        assert report.carbon_trends.avg_intensity_gco2_kwh.value == 215.0
        assert report.carbon_trends.min_intensity_gco2_kwh.value == 85.0
        assert report.carbon_trends.max_intensity_gco2_kwh.value == 420.0

    def test_workload_utilization_measured(self, full_workload_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[],
            workload_summary=full_workload_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        wu = report.workload_utilization
        assert wu.avg_cpu_request_ratio.measured is True
        assert wu.avg_cpu_request_ratio.value == 0.45
        assert wu.avg_replica_count.value == 2.3

    def test_optimization_events_populated(self, full_carbon_summary):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert len(report.optimization_events) == 1
        ev = report.optimization_events[0]
        assert ev.action == "SCALE_DOWN"
        assert ev.pre_replicas == 3
        assert ev.recommended_replicas == 1
        assert ev.policy_status == "APPROVED"
        assert ev.gitops_status == "PR_CREATED"
        assert ev.final_outcome == "SUCCESS"

    def test_pre_post_health_captured(self, full_carbon_summary):
        lc = make_lifecycle(pre_cpu=0.40, post_cpu=0.55, pre_p99=0.12, post_p99=0.18)
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ev = report.optimization_events[0]
        assert ev.pre_cpu_ratio == 0.40
        assert ev.post_cpu_ratio == 0.55
        assert ev.pre_memory_ratio == 0.45
        assert ev.post_memory_ratio == 0.60
        assert ev.pre_request_rate == 1.0
        assert ev.post_request_rate == 0.9
        assert ev.pre_p99_latency == 0.12
        assert ev.post_p99_latency == 0.18

    def test_impact_estimates_with_carbon(self, full_carbon_summary):
        lc = make_lifecycle(
            current_replicas=3,
            recommended_replicas=1,
            duration_hours=4.0,
        )
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        # replica_hours_saved = (3 - 1) × 4h = 8.0 replica·hours
        assert ie.total_replica_hours_saved.available
        assert ie.total_replica_hours_saved.measured is False
        assert ie.total_replica_hours_saved.provenance is ValueProvenance.CALCULATED
        assert ie.total_replica_hours_saved.value == pytest.approx(8.0, rel=0.01)

        # cpu_hours = 8.0 × 0.2 = 1.6
        assert ie.estimated_cpu_hours_saved.measured is False
        assert ie.estimated_cpu_hours_saved.provenance is ValueProvenance.ESTIMATED
        assert ie.estimated_cpu_hours_saved.value == pytest.approx(1.6, rel=0.01)

        # kwh = 1.6 × 10 / 1000 = 0.016
        assert ie.estimated_kwh_saved.value == pytest.approx(0.016, rel=0.01)

        # CO2 = 0.016 × 215.0 = 3.44 gCO2
        assert ie.estimated_co2_grams_avoided.available
        assert ie.estimated_co2_grams_avoided.measured is False
        assert ie.estimated_co2_grams_avoided.provenance is ValueProvenance.ESTIMATED
        assert ie.estimated_co2_grams_avoided.value == pytest.approx(3.44, rel=0.05)

        # Cost = 1.6 × 0.035 = 0.056
        assert ie.estimated_cost_saved_usd.value == pytest.approx(0.056, rel=0.01)

    def test_rollback_event_counted(self, full_carbon_summary):
        lc = make_lifecycle(
            final_outcome="ROLLBACK_PREPARED",
            verification_outcome="ROLLBACK_REQUIRED",
            rollback=True,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.total_rollbacks == 1
        ev = report.optimization_events[0]
        assert ev.had_rollback is True
        assert ev.rollback_branch == "greenops/rollback"

    def test_rollback_event_does_not_claim_savings(self, full_carbon_summary):
        lc = make_lifecycle(
            final_outcome="ROLLBACK_PREPARED",
            verification_outcome="ROLLBACK_REQUIRED",
            rollback=True,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        assert ie.total_replica_hours_saved.available is False
        assert ie.estimated_cpu_hours_saved.available is False
        assert ie.estimated_kwh_saved.available is False
        assert ie.estimated_co2_grams_avoided.available is False
        assert ie.estimated_cost_saved_usd.available is False

    def test_degraded_event_does_not_claim_verified_savings(self, full_carbon_summary):
        lc = make_lifecycle(
            final_outcome="DEGRADED",
            verification_outcome="DEGRADED",
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.impact_estimates.total_replica_hours_saved.available is False

    def test_report_to_dict_serializable(self, full_carbon_summary, full_workload_summary):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
            workload_summary=full_workload_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        d = report.to_dict()
        # Must be JSON-serializable (no Pydantic objects, datetimes, etc.)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        assert len(serialized) > 100


# ===========================================================================
# TestWeeklyReportGeneratorPartial
# ===========================================================================


class TestWeeklyReportGeneratorPartial:
    """Partial data — some sections have data, some don't."""

    def test_no_carbon_summary(self):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(lifecycles=[lc])
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.carbon_trends.avg_intensity_gco2_kwh.available is False
        assert report.carbon_trends.avg_intensity_gco2_kwh.measured is False
        assert any("Carbon intensity" in n for n in report.data_quality_notes)

    def test_no_workload_summary(self, full_carbon_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        wu = report.workload_utilization
        assert wu.avg_cpu_request_ratio.available is False
        assert any("Workload utilization" in n for n in report.data_quality_notes)

    def test_co2_unavailable_without_carbon_data(self):
        lc = make_lifecycle(duration_hours=4.0)
        gen = WeeklyReportGenerator(lifecycles=[lc])
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        # Replica hours are still calculable
        assert ie.total_replica_hours_saved.available
        # But CO2 needs avg carbon intensity
        assert ie.estimated_co2_grams_avoided.available is False
        assert any("CO2" in n for n in report.data_quality_notes)

    def test_partial_reporting_period_keeps_only_observed_values_available(self):
        gen = WeeklyReportGenerator(
            lifecycles=[],
            carbon_summary={"avg_intensity": 200.0},
            workload_summary={"avg_cpu_ratio": 0.40, "avg_replicas": 2.0},
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.carbon_trends.avg_intensity_gco2_kwh.provenance is ValueProvenance.MEASURED
        assert report.carbon_trends.min_intensity_gco2_kwh.provenance is ValueProvenance.UNAVAILABLE
        assert (
            report.workload_utilization.avg_cpu_request_ratio.provenance is ValueProvenance.MEASURED
        )
        assert (
            report.workload_utilization.avg_memory_request_ratio.provenance
            is ValueProvenance.UNAVAILABLE
        )
        assert report.impact_estimates.estimated_co2_grams_avoided.available is False
        assert any(
            "Missing values are reported as unavailable" in n for n in report.data_quality_notes
        )

    def test_rejected_lifecycle_counted(self):
        lc = make_lifecycle(
            policy_status="REJECTED",
            gitops_status=None,
            final_outcome="BLOCKED:REJECTED",
            verification_outcome=None,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc])
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.total_rejected == 1
        assert report.total_applied == 0

    def test_deferred_lifecycle_counted(self):
        lc = make_lifecycle(
            action="DEFER",
            policy_status="APPROVED",
            gitops_status=None,
            final_outcome="DEFERRED",
            verification_outcome=None,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc])
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.total_deferred == 1


# ===========================================================================
# TestWeeklyReportGeneratorEmpty
# ===========================================================================


class TestWeeklyReportGeneratorEmpty:
    """No data — the report must not fabricate values."""

    def test_empty_report(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert report.total_optimization_cycles == 0
        assert report.total_applied == 0
        assert report.total_rollbacks == 0

        ie = report.impact_estimates
        assert ie.total_replica_hours_saved.available is False
        assert ie.estimated_cpu_hours_saved.available is False
        assert ie.estimated_kwh_saved.available is False
        assert ie.estimated_co2_grams_avoided.available is False
        assert ie.estimated_cost_saved_usd.available is False
        assert ie.total_replica_hours_saved.provenance is ValueProvenance.UNAVAILABLE

    def test_empty_carbon_trends(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ct = report.carbon_trends
        assert ct.avg_intensity_gco2_kwh.available is False
        assert ct.min_intensity_gco2_kwh.available is False
        assert ct.max_intensity_gco2_kwh.available is False

    def test_data_quality_notes_explain_missing_data(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert len(report.data_quality_notes) >= 2
        notes_text = " ".join(report.data_quality_notes)
        assert "Carbon" in notes_text
        assert "Workload" in notes_text


# ===========================================================================
# TestImpactEstimationChain
# ===========================================================================


class TestImpactEstimationChain:
    """Step-by-step verification of the estimation chain."""

    def test_scale_up_not_counted_as_savings(self, full_carbon_summary):
        """Only scale-downs produce savings — scale-ups are ignored."""
        lc = make_lifecycle(
            action="SCALE_UP",
            current_replicas=1,
            recommended_replicas=3,
            duration_hours=4.0,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        assert not ie.total_replica_hours_saved.available

    def test_blocked_event_not_counted(self, full_carbon_summary):
        lc = make_lifecycle(
            gitops_status="BLOCKED",
            final_outcome="BLOCKED",
            duration_hours=1.0,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        assert not ie.total_replica_hours_saved.available

    def test_custom_estimation_config(self, full_carbon_summary):
        cfg = ReportEstimationConfig(
            watts_per_cpu_core=20.0,
            hourly_cost_per_cpu_core_usd=0.10,
            default_cpu_per_replica_cores=0.5,
        )
        lc = make_lifecycle(
            current_replicas=3,
            recommended_replicas=1,
            duration_hours=2.0,
        )
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
            estimation_config=cfg,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        # replica_hours = 2 × 2h = 4.0
        assert ie.total_replica_hours_saved.value == pytest.approx(4.0, rel=0.01)
        # cpu_hours = 4.0 × 0.5 = 2.0
        assert ie.estimated_cpu_hours_saved.value == pytest.approx(2.0, rel=0.01)
        # kwh = 2.0 × 20 / 1000 = 0.04
        assert ie.estimated_kwh_saved.value == pytest.approx(0.04, rel=0.01)
        # CO2 = 0.04 × 215.0 = 8.6
        assert ie.estimated_co2_grams_avoided.value == pytest.approx(8.6, rel=0.05)
        # Cost = 2.0 × 0.10 = 0.20
        assert ie.estimated_cost_saved_usd.value == pytest.approx(0.20, rel=0.01)

    def test_estimation_config_embedded_in_report(self, full_carbon_summary):
        gen = WeeklyReportGenerator(lifecycles=[], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        assert "watts_per_cpu_core" in report.estimation_config
        assert report.estimation_config["watts_per_cpu_core"] == 10.0

    def test_multiple_lifecycles_summed(self, full_carbon_summary):
        lc1 = make_lifecycle(
            current_replicas=3,
            recommended_replicas=1,
            duration_hours=2.0,
        )
        lc2 = make_lifecycle(
            current_replicas=4,
            recommended_replicas=2,
            duration_hours=3.0,
        )
        gen = WeeklyReportGenerator(
            lifecycles=[lc1, lc2],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        ie = report.impact_estimates
        # lc1: 2 × 2h = 4.0; lc2: 2 × 3h = 6.0; total = 10.0
        assert ie.total_replica_hours_saved.value == pytest.approx(10.0, rel=0.01)


# ===========================================================================
# TestMarkdownRenderer
# ===========================================================================


class TestMarkdownRenderer:
    def test_renders_to_markdown(self, full_carbon_summary, full_workload_summary):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
            workload_summary=full_workload_summary,
            region="DE",
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert isinstance(md, str)
        assert "# GreenOps Weekly Report" in md
        assert "## Carbon Intensity Trends" in md
        assert "## Kubernetes Workload Utilization" in md
        assert "## Optimization Summary" in md
        assert "## Optimization Events" in md
        assert "## Estimated Impact" in md

    def test_provenance_markers_present(self, full_carbon_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[make_lifecycle()],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "[M]" in md  # measured values
        assert "[CALC]" in md  # calculated values
        assert "[EST]" in md  # estimated values

    def test_legend_present(self, full_carbon_summary):
        gen = WeeklyReportGenerator(carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "[M] = measured" in md
        assert "[CALC] = calculated" in md
        assert "[EST] = estimated" in md

    def test_estimation_warning_present(self, full_carbon_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[make_lifecycle()],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "estimates" in md.lower()
        assert "not direct measurements" in md.lower()

    def test_unavailable_shows_dash(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "data unavailable" in md

    def test_rollback_event_rendered(self, full_carbon_summary):
        lc = make_lifecycle(
            final_outcome="ROLLBACK_PREPARED",
            verification_outcome="ROLLBACK_REQUIRED",
            rollback=True,
        )
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "🔄" in md  # rollback emoji
        assert "greenops/rollback" in md

    def test_pre_post_health_table_rendered(self, full_carbon_summary):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(lifecycles=[lc], carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "Before" in md
        assert "After" in md
        assert "CPU ratio" in md
        assert "Memory ratio" in md
        assert "Request rate" in md

    def test_empty_events_shows_message(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "No optimization events" in md

    def test_data_quality_notes_rendered(self):
        gen = WeeklyReportGenerator()
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "## Data Quality Notes" in md

    def test_estimation_config_rendered(self, full_carbon_summary):
        gen = WeeklyReportGenerator(
            lifecycles=[make_lifecycle()],
            carbon_summary=full_carbon_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)
        md = render_markdown(report)

        assert "### Estimation Config" in md
        assert "watts_per_cpu_core" in md


# ===========================================================================
# TestDataQualityNotes
# ===========================================================================


class TestDataQualityNotes:
    def test_no_notes_when_all_data_present(self, full_carbon_summary, full_workload_summary):
        lc = make_lifecycle()
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
            workload_summary=full_workload_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        # Should have no data quality notes (all data present)
        assert len(report.data_quality_notes) == 0

    def test_notes_for_missing_carbon(self, full_workload_summary):
        gen = WeeklyReportGenerator(workload_summary=full_workload_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        carbon_notes = [n for n in report.data_quality_notes if "Carbon" in n]
        assert len(carbon_notes) >= 1

    def test_notes_for_missing_workload(self, full_carbon_summary):
        gen = WeeklyReportGenerator(carbon_summary=full_carbon_summary)
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        workload_notes = [n for n in report.data_quality_notes if "Workload" in n]
        assert len(workload_notes) >= 1

    def test_notes_for_no_scale_downs(self, full_carbon_summary, full_workload_summary):
        # Scale-up only — no savings possible
        lc = make_lifecycle(
            action="SCALE_UP",
            current_replicas=1,
            recommended_replicas=3,
        )
        gen = WeeklyReportGenerator(
            lifecycles=[lc],
            carbon_summary=full_carbon_summary,
            workload_summary=full_workload_summary,
        )
        report = gen.generate(period_start=PERIOD_START, period_end=PERIOD_END)

        impact_notes = [n for n in report.data_quality_notes if "scale-down" in n]
        assert len(impact_notes) >= 1
