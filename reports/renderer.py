"""
reports/renderer.py — Markdown rendering for the WeeklyGreenOpsReport.

Produces structured Markdown that can be:
  - Displayed in a terminal or Git commit body
  - Converted to HTML via any Markdown processor
  - Converted to PDF via pandoc, WeasyPrint, or similar

The renderer never invents data. Unavailable values are shown as "—" with
a "(data unavailable)" label. Estimated values carry an "[EST]" marker.
Measured values carry a "[M]" marker. Calculated values carry a "[CALC]" marker.
"""

from __future__ import annotations

from datetime import UTC, datetime

from reports.models import (
    ReportValue,
    ValueProvenance,
    WeeklyGreenOpsReport,
)


def render_markdown(report: WeeklyGreenOpsReport) -> str:
    """Render a complete WeeklyGreenOpsReport as Markdown."""
    sections = [
        _header(report),
        _carbon_trends(report),
        _workload_utilization(report),
        _optimization_summary(report),
        _optimization_detail(report),
        _impact_estimates(report),
        _data_quality(report),
        _footer(report),
    ]
    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _v(rv: ReportValue, fmt: str = ".2f") -> str:
    """Format a ReportValue with provenance marker."""
    if rv.value is None:
        return "— *(data unavailable)*"
    tags: dict[ValueProvenance, str] = {
        ValueProvenance.MEASURED: "[M]",
        ValueProvenance.CALCULATED: "[CALC]",
        ValueProvenance.ESTIMATED: "[EST]",
    }
    tag = tags.get(rv.provenance or ValueProvenance.ESTIMATED, "[EST]")
    formatted = f"{rv.value:{fmt}}"
    unit = f" {rv.unit}" if rv.unit else ""
    return f"{formatted}{unit} {tag}"


def _ts(epoch: float | None) -> str:
    """Format a Unix timestamp to ISO 8601."""
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(r: WeeklyGreenOpsReport) -> str:
    return "\n".join(
        [
            "# GreenOps Weekly Report",
            "",
            f"**Report ID:** `{r.report_id}`",
            f"**Period:** {r.period_start} → {r.period_end}",
            f"**Region:** {r.region or '*(not specified)*'}",
            f"**Deployment:** `{r.deployment}` in `{r.namespace}`",
            f"**Generated:** {r.generated_at}",
            "",
            "---",
            "",
            "> **Legend:** [M] = measured value · [CALC] = calculated from measured "
            "inputs · [EST] = estimated using documented assumptions · — = data unavailable",
        ]
    )


def _carbon_trends(r: WeeklyGreenOpsReport) -> str:
    ct = r.carbon_trends
    return "\n".join(
        [
            "## Carbon Intensity Trends",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Average intensity | {_v(ct.avg_intensity_gco2_kwh)} |",
            f"| Minimum intensity | {_v(ct.min_intensity_gco2_kwh)} |",
            f"| Maximum intensity | {_v(ct.max_intensity_gco2_kwh)} |",
            f"| Average renewable share | {_v(ct.avg_renewable_pct)} |",
            f"| Average fossil share | {_v(ct.avg_fossil_pct)} |",
            f"| Data availability | {_v(ct.data_availability_pct)} |",
        ]
    )


def _workload_utilization(r: WeeklyGreenOpsReport) -> str:
    wu = r.workload_utilization
    return "\n".join(
        [
            "## Kubernetes Workload Utilization",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Average CPU request ratio | {_v(wu.avg_cpu_request_ratio)} |",
            f"| Average memory request ratio | {_v(wu.avg_memory_request_ratio)} |",
            f"| Average replica count | {_v(wu.avg_replica_count)} |",
            f"| Average request rate | {_v(wu.avg_request_rate_rps)} |",
            f"| Average P99 latency | {_v(wu.avg_p99_latency_seconds, '.3f')} |",
            f"| Total errors | {_v(wu.total_error_count, '.0f')} |",
            f"| Average availability | {_v(wu.avg_availability_ratio, '.3f')} |",
        ]
    )


def _optimization_summary(r: WeeklyGreenOpsReport) -> str:
    return "\n".join(
        [
            "## Optimization Summary",
            "",
            "| Metric | Count |",
            "|---|---|",
            f"| Total optimization cycles | {r.total_optimization_cycles} |",
            f"| Applied (GitOps change prepared) | {r.total_applied} |",
            f"| Approved by policy | {r.total_approved} |",
            f"| Rejected by policy | {r.total_rejected} |",
            f"| Deferred | {r.total_deferred} |",
            f"| Rollbacks triggered | {r.total_rollbacks} |",
            f"| Errors | {r.total_errors} |",
        ]
    )


def _optimization_detail(r: WeeklyGreenOpsReport) -> str:
    if not r.optimization_events:
        return "## Optimization Events\n\n*No optimization events in this period.*"

    lines = [
        "## Optimization Events",
        "",
    ]

    for i, ev in enumerate(r.optimization_events, 1):
        status_emoji = {
            "SUCCESS": "✅",
            "DEGRADED": "⚠️",
            "ROLLBACK_PREPARED": "🔄",
            "ROLLBACK_FAILED": "❌",
            "BLOCKED": "🚫",
            "DEFERRED": "⏳",
            "NO_ACTION": "➖",
            "INCONCLUSIVE": "❓",
            "ERROR": "💥",
        }.get(ev.final_outcome, "•")

        lines.append(f"### {i}. {status_emoji} {ev.final_outcome}")
        lines.append("")
        lines.append(f"- **Lifecycle ID:** `{ev.lifecycle_id}`")
        lines.append(f"- **Time:** {_ts(ev.started_at)} → {_ts(ev.completed_at)}")
        lines.append(f"- **Action:** `{ev.action}`")
        lines.append(f"- **Reason:** {ev.reason or '*(none)*'}")
        if ev.confidence is not None:
            lines.append(f"- **Confidence:** {ev.confidence:.0%}")
        if ev.decision_basis:
            lines.append(f"- **Decision basis:** `{ev.decision_basis}`")

        # Policy
        lines.append(f"- **Policy status:** `{ev.policy_status}`")
        if ev.policy_reason:
            lines.append(f"- **Policy reason:** {ev.policy_reason}")
        if ev.safeguards_triggered:
            lines.append(f"- **Safeguards triggered:** {', '.join(ev.safeguards_triggered)}")

        # Replicas
        if ev.pre_replicas is not None or ev.recommended_replicas is not None:
            lines.append(
                f"- **Replicas:** {ev.pre_replicas or '?'} → "
                f"{ev.recommended_replicas or '?'} (recommended) → "
                f"{ev.post_replicas or '?'} (actual)"
            )

        # GitOps
        if ev.gitops_status:
            lines.append(f"- **GitOps status:** `{ev.gitops_status}`")
        if ev.gitops_pr_url:
            lines.append(f"- **PR:** [{ev.gitops_pr_url}]({ev.gitops_pr_url})")

        # Pre/post health comparison
        if ev.was_applied and any(
            [
                ev.pre_cpu_ratio,
                ev.post_cpu_ratio,
                ev.pre_memory_ratio,
                ev.post_memory_ratio,
                ev.pre_request_rate,
                ev.post_request_rate,
                ev.pre_p99_latency,
                ev.post_p99_latency,
            ]
        ):
            lines.append("")
            lines.append("  | Metric | Before | After |")
            lines.append("  |---|---|---|")
            if ev.pre_cpu_ratio is not None or ev.post_cpu_ratio is not None:
                lines.append(f"  | CPU ratio | {_f(ev.pre_cpu_ratio)} | {_f(ev.post_cpu_ratio)} |")
            if ev.pre_p99_latency is not None or ev.post_p99_latency is not None:
                lines.append(
                    f"  | P99 latency | {_f(ev.pre_p99_latency, 's')} | "
                    f"{_f(ev.post_p99_latency, 's')} |"
                )
            if ev.pre_memory_ratio is not None or ev.post_memory_ratio is not None:
                lines.append(
                    f"  | Memory ratio | {_f(ev.pre_memory_ratio)} | {_f(ev.post_memory_ratio)} |"
                )
            if ev.pre_request_rate is not None or ev.post_request_rate is not None:
                lines.append(
                    f"  | Request rate | {_f(ev.pre_request_rate, ' rps')} | "
                    f"{_f(ev.post_request_rate, ' rps')} |"
                )
            if ev.pre_availability is not None or ev.post_availability is not None:
                lines.append(
                    f"  | Availability | {_f(ev.pre_availability)} | {_f(ev.post_availability)} |"
                )
            if ev.pre_error_rate is not None or ev.post_error_rate is not None:
                lines.append(
                    f"  | Error rate | {_f(ev.pre_error_rate, ' rps')} | "
                    f"{_f(ev.post_error_rate, ' rps')} |"
                )

        # Verification
        if ev.verification_outcome:
            lines.append(f"- **Verification:** `{ev.verification_outcome}`")
        if ev.verification_reason:
            lines.append(f"- **Verification reason:** {ev.verification_reason}")
        if ev.safety_violations:
            lines.append(f"- **Safety violations:** {', '.join(ev.safety_violations)}")

        # Rollback
        if ev.had_rollback:
            lines.append(f"- **Rollback branch:** `{ev.rollback_branch}`")
            if ev.rollback_pr_url:
                lines.append(f"- **Rollback PR:** [{ev.rollback_pr_url}]({ev.rollback_pr_url})")

        lines.append("")

    return "\n".join(lines)


def _impact_estimates(r: WeeklyGreenOpsReport) -> str:
    ie = r.impact_estimates
    lines = [
        "## Estimated Impact",
        "",
        "> ⚠️ Values in this section are **estimates** derived from measured replica "
        "changes and configurable assumptions. They are not direct measurements. "
        "See *Estimation Config* below for the constants used.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Replica·hours saved | {_v(ie.total_replica_hours_saved, '.2f')} |",
        f"| CPU·hours saved | {_v(ie.estimated_cpu_hours_saved, '.2f')} |",
        f"| Energy saved | {_v(ie.estimated_kwh_saved, '.4f')} |",
        f"| CO₂ avoided | {_v(ie.estimated_co2_grams_avoided, '.2f')} |",
        f"| Cost saved | {_v(ie.estimated_cost_saved_usd, '.4f')} |",
    ]

    # Notes on each estimate
    for field_name in [
        "total_replica_hours_saved",
        "estimated_cpu_hours_saved",
        "estimated_kwh_saved",
        "estimated_co2_grams_avoided",
        "estimated_cost_saved_usd",
    ]:
        rv: ReportValue = getattr(ie, field_name)
        if rv.note:
            lines.append(f"\n*{field_name}:* {rv.note}")

    # Estimation config
    cfg = r.estimation_config
    if cfg:
        lines.extend(
            [
                "",
                "### Estimation Config",
                "",
                "| Parameter | Value |",
                "|---|---|",
            ]
        )
        for k, v in cfg.items():
            lines.append(f"| `{k}` | {v} |")

    return "\n".join(lines)


def _data_quality(r: WeeklyGreenOpsReport) -> str:
    if not r.data_quality_notes:
        return ""
    lines = [
        "## Data Quality Notes",
        "",
    ]
    for note in r.data_quality_notes:
        lines.append(f"- {note}")
    return "\n".join(lines)


def _footer(r: WeeklyGreenOpsReport) -> str:
    return "\n".join(
        [
            "---",
            "",
            f"*Report generated by GreenOps AI · `{r.report_id}`*",
        ]
    )


def _f(v: float | None, suffix: str = "") -> str:
    """Format a float or show '—'."""
    if v is None:
        return "—"
    return f"{v:.3f}{suffix}"
