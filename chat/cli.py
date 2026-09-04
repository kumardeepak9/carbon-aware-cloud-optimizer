"""CLI for the GreenOps chat query interface.

    python -m chat.cli "what decisions did you make last week?"
    python -m chat.cli --json "were any recommendations rejected by policy?"

Reads the decision-history store (config: REPORT_DECISION_HISTORY_PATH) and,
when reachable, the Prometheus backend (PROMETHEUS_API_URL). If Prometheus is
unreachable the interface still answers history-only questions and says so for
metric-only ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from chat.history import DecisionHistoryStore
from chat.interface import GreenOpsChat
from chat.models import GroundedAnswer
from chat.retriever import HistoryRetriever, MetricRetriever
from config import bootstrap
from config.settings import PrometheusSettings, ReportingSettings
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat.cli", description="Ask GreenOps about its optimization history."
    )
    p.add_argument("question", nargs="+", help="the question to ask")
    p.add_argument(
        "--json", action="store_true", help="print the structured GroundedAnswer as JSON"
    )
    p.add_argument("--no-metrics", action="store_true", help="do not attempt to reach Prometheus")
    p.add_argument("--history-path", default=None, help="override the decision-history JSONL path")
    return p


async def _run(args: argparse.Namespace) -> int:
    reporting = ReportingSettings()
    store = DecisionHistoryStore(args.history_path or reporting.decision_history_path)
    history = HistoryRetriever(store)
    question = " ".join(args.question)

    if args.no_metrics:
        answer = await GreenOpsChat(history).ask(question)
        _emit(answer, as_json=args.json)
        return 0 if answer.answered else 2

    prom = PrometheusClient(base_url=PrometheusSettings().api_url)
    await prom.open()
    try:
        metrics = MetricRetriever(prom, GreenOpsQueries())
        answer = await GreenOpsChat(history, metrics).ask(question)
    finally:
        await prom.close()
    _emit(answer, as_json=args.json)
    return 0 if answer.answered else 2


def _emit(answer: GroundedAnswer, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(answer.to_dict(), indent=2, default=str))
        return
    print(answer.text)
    if answer.evidence:
        print(f"\n— grounded in {len(answer.evidence)} record(s):")
        for e in answer.evidence:
            print(f"  · {e.source}:{e.ref}")
    if not answer.data_complete:
        print("\n[partial: some requested data was unavailable]")


def main() -> None:
    bootstrap()
    args = _parser().parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
