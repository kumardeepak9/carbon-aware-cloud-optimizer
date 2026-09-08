#!/usr/bin/env python3
# ruff: noqa: E402
"""Interactive GreenOps AI chat helper.

This script wraps the existing retrieval-grounded chat interface so local
developers can ask repeated questions without re-running `make chat` each time.
It reads `.env`, decision history, and Prometheus using the same project
configuration as the rest of GreenOps.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

if VENV_PYTHON.exists() and Path(sys.executable).absolute() != VENV_PYTHON:
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

os.chdir(REPO_ROOT)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chat.history import DecisionHistoryStore
from chat.interface import GreenOpsChat
from chat.models import GroundedAnswer
from chat.retriever import HistoryRetriever, MetricRetriever
from config import bootstrap
from config.settings import KubernetesSettings, PrometheusSettings, ReportingSettings
from monitoring.client import PrometheusClient
from monitoring.queries import GreenOpsQueries

SUGGESTED_QUESTIONS = (
    "What is the current carbon intensity?",
    "What decisions did you make this week?",
    "Why did the last scale-down happen?",
    "Were any recommendations rejected by policy?",
    "Did latency change after the last optimization?",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Chat interactively with the GreenOps AI agent.",
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="optional single question; omit it to start interactive chat",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="answer from decision history only; do not query Prometheus",
    )
    parser.add_argument(
        "--history-path",
        default=None,
        help="override the decision-history JSONL path",
    )
    return parser


def _print_answer(answer: GroundedAnswer) -> None:
    print(answer.text)
    if answer.evidence:
        print(f"\nGrounded in {len(answer.evidence)} record(s):")
        for evidence in answer.evidence:
            print(f"  - {evidence.source}:{evidence.ref}")
    if not answer.data_complete:
        print("\nPartial answer: some requested data was unavailable.")


async def _build_chat(args: argparse.Namespace) -> tuple[GreenOpsChat, PrometheusClient | None]:
    reporting = ReportingSettings()
    history_path = args.history_path or reporting.decision_history_path
    history = HistoryRetriever(DecisionHistoryStore(history_path))

    if args.no_metrics:
        return GreenOpsChat(history), None

    prometheus = PrometheusClient(base_url=PrometheusSettings().api_url)
    await prometheus.open()
    kubernetes = KubernetesSettings()
    queries = GreenOpsQueries(namespace=kubernetes.namespace)
    metrics = MetricRetriever(prometheus, queries)
    return GreenOpsChat(history, metrics), prometheus


async def _run_once(chat: GreenOpsChat, question: str) -> int:
    answer = await chat.ask(question)
    _print_answer(answer)
    return 0 if answer.answered else 2


async def _run_interactive(chat: GreenOpsChat) -> int:
    print("GreenOps AI chat")
    print("Type a question, or `exit` to quit.\n")
    print("Try:")
    for question in SUGGESTED_QUESTIONS:
        print(f"  - {question}")
    print()

    while True:
        try:
            question = input("greenops> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if question.lower() in {"exit", "quit", "q"}:
            return 0
        if not question:
            continue

        print()
        answer = await chat.ask(question)
        _print_answer(answer)
        print()


async def _run(args: argparse.Namespace) -> int:
    chat, prometheus = await _build_chat(args)
    try:
        if args.question:
            return await _run_once(chat, " ".join(args.question))
        return await _run_interactive(chat)
    finally:
        if prometheus is not None:
            await prometheus.close()


def main() -> None:
    bootstrap()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
