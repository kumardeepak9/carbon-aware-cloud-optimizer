"""CLI entry point for generating a weekly GreenOps report."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chat.history import DecisionHistoryStore
from chat.models import TimeRange
from chat.retriever import MetricRetriever
from config import bootstrap, get_logger
from config.settings import PrometheusSettings, ReportingSettings
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries
from reports.generator import WeeklyReportGenerator, event_from_decision_record
from reports.models import OptimizationEventRecord
from reports.renderer import render_markdown

log = get_logger(__name__)


def _load_events(
    history_path: str, start: datetime, end: datetime
) -> list[OptimizationEventRecord]:
    """Load decision-history records that started within [start, end)."""
    store = DecisionHistoryStore(history_path)
    if not store.exists:
        log.warning("report.no_decision_history", path=history_path)
        return []
    records, skipped = store.load()
    if skipped:
        log.warning("report.history_records_skipped", path=history_path, skipped=skipped)
    lo, hi = start.timestamp(), end.timestamp()
    return [event_from_decision_record(r) for r in records if lo <= r.started_at < hi]


async def _load_metric_summaries(
    prometheus_url: str, namespace: str, deployment: str, start: datetime, end: datetime
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Aggregate carbon + workload time-series over the reporting window.

    Returns ``(carbon_summary, workload_summary)`` dicts in the shape
    ``WeeklyReportGenerator`` expects. Missing/unreachable metrics are simply
    left out — the report renders them as "data unavailable", never fabricated.
    """
    carbon: dict[str, float | None] = {}
    workload: dict[str, float | None] = {}
    window = TimeRange(start=start, end=end, label="reporting period")
    q = GreenOpsQueries(namespace=namespace, deployment=deployment)

    client = PrometheusClient(base_url=prometheus_url)
    try:
        await client.open()
        if not await client.is_healthy():
            log.warning("report.prometheus_unreachable", url=prometheus_url)
            return carbon, workload
        mr = MetricRetriever(client, q)

        async def agg(metric: str, expr: str) -> tuple[float | None, float | None, float | None]:
            w = await mr.series(metric, expr, window, step="15m")
            return (w.mean, w.minimum, w.maximum) if w.available else (None, None, None)

        ci_avg, ci_min, ci_max = await agg("carbon_intensity", q.carbon_intensity_gco2_kwh().expr)
        carbon["avg_intensity"], carbon["min_intensity"], carbon["max_intensity"] = (
            ci_avg, ci_min, ci_max,
        )
        ren_avg, _, _ = await agg("renewable", q.renewable_percentage().expr)
        carbon["avg_renewable_pct"] = ren_avg
        avail_avg, _, _ = await agg("carbon_data_available", q.carbon_data_available().expr)
        carbon["data_availability_pct"] = None if avail_avg is None else avail_avg * 100.0

        cpu_avg, _, _ = await agg("cpu_request_ratio", q.cpu_request_ratio().expr)
        workload["avg_cpu_ratio"] = cpu_avg
        mem_avg, _, _ = await agg("memory_request_ratio", q.memory_request_ratio().expr)
        workload["avg_memory_ratio"] = mem_avg
        rep_avg, _, _ = await agg("replica_count_desired", q.replica_count_desired().expr)
        workload["avg_replicas"] = rep_avg
        rr_avg, _, _ = await agg("http_request_rate_rps", q.http_request_rate().expr)
        workload["avg_request_rate"] = rr_avg
    except Exception as exc:  # noqa: BLE001 — a metrics failure must not fail the report
        log.warning("report.metric_summary_failed", error=str(exc))
    finally:
        with contextlib.suppress(Exception):
            await client.close()

    return carbon, workload


def _default_period() -> tuple[datetime, datetime]:
    """Return the most recent seven-day UTC reporting window."""
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=7)
    return start, end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a weekly GreenOps Markdown report.")
    parser.add_argument("--output-dir", default=None, help="Directory for the generated report.")
    parser.add_argument("--region", default="", help="Carbon region/zone label for the report.")
    parser.add_argument("--namespace", default="greenops", help="Kubernetes namespace.")
    parser.add_argument(
        "--deployment",
        default="greenops-demo-workload",
        help="Kubernetes Deployment name.",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="Skip Prometheus trend queries; use decision history only.",
    )
    return parser


def main() -> None:
    """Generate a weekly report from decision history + Prometheus trends."""
    args = build_parser().parse_args()
    bootstrap()
    settings = ReportingSettings()
    output_dir = Path(args.output_dir or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    period_start, period_end = _default_period()
    events = _load_events(settings.decision_history_path, period_start, period_end)

    carbon_summary: dict[str, float | None] = {}
    workload_summary: dict[str, float | None] = {}
    if not args.no_metrics:
        carbon_summary, workload_summary = asyncio.run(
            _load_metric_summaries(
                PrometheusSettings().api_url,
                args.namespace,
                args.deployment,
                period_start,
                period_end,
            )
        )

    report = WeeklyReportGenerator(
        events=events,
        carbon_summary=carbon_summary,
        workload_summary=workload_summary,
        region=args.region,
        namespace=args.namespace,
        deployment=args.deployment,
    ).generate(period_start=period_start, period_end=period_end)

    output_path = output_dir / f"greenops-weekly-{period_end.date().isoformat()}.md"
    output_path.write_text(render_markdown(report), encoding="utf-8")
    print(output_path.as_posix())


if __name__ == "__main__":
    main()
