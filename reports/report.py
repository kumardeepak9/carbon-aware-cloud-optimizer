"""CLI entry point for generating a weekly GreenOps report."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import ReportingSettings
from reports.generator import WeeklyReportGenerator
from reports.renderer import render_markdown


def _default_period() -> tuple[datetime, datetime]:
    """Return the most recent seven-day UTC reporting window."""
    end = datetime.now(timezone.utc).replace(microsecond=0)
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
    return parser


def main() -> None:
    """Generate a report with available local lifecycle data."""
    args = build_parser().parse_args()
    settings = ReportingSettings()
    output_dir = Path(args.output_dir or settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    period_start, period_end = _default_period()
    report = WeeklyReportGenerator(
        region=args.region,
        namespace=args.namespace,
        deployment=args.deployment,
    ).generate(period_start=period_start, period_end=period_end)

    output_path = output_dir / f"greenops-weekly-{period_end.date().isoformat()}.md"
    output_path.write_text(render_markdown(report), encoding="utf-8")
    print(output_path.as_posix())


if __name__ == "__main__":
    main()
