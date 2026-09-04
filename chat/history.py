"""Append-only decision-history store for the GreenOps chat interface.

Each completed ``OptimizationLifecycle`` is flattened into a ``DecisionRecord``
and appended as one JSON line. The chat interface reads these back — it never
reconstructs a decision from anything other than a stored record.

The store is deliberately simple (one JSONL file, whole-file scan). Volume is
one record per agent poll cycle; a JSONL file stays small and is trivially
auditable. Corrupt lines are skipped, not fatal — a partial history is still
better than none, and the reader reports incompleteness.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from config import get_logger

log = get_logger(__name__)


class DecisionRecord(BaseModel):
    """A flat, queryable projection of one optimization lifecycle.

    Only fields the chat interface can answer questions from. Every value is
    copied verbatim from the lifecycle — nothing is derived or inferred here.
    """

    lifecycle_id: str
    started_at: float
    completed_at: float | None = None

    # Decision
    action: str = ""
    reason: str = ""
    decision_basis: str = ""
    confidence: float | None = None
    current_replicas: int | None = None
    recommended_replicas: int | None = None

    # Policy validation
    policy_status: str = ""
    policy_reason: str = ""
    safeguards_triggered: list[str] = Field(default_factory=list)
    approved_for_gitops_change: bool = False

    # Carbon context captured at decision time
    carbon_intensity_gco2_kwh: float | None = None
    carbon_region: str | None = None
    renewable_percentage: float | None = None
    fossil_fuel_percentage: float | None = None
    carbon_data_available: bool | None = None
    carbon_data_timestamp_seconds: float | None = None

    # Operational context at decision time
    obs_cpu_request_ratio: float | None = None
    obs_p99_latency_seconds: float | None = None
    obs_error_rate_rps: float | None = None
    obs_availability_ratio: float | None = None

    # Pre / post verification snapshots (present only for applied changes)
    pre_snapshot: dict[str, Any] = Field(default_factory=dict)
    post_snapshot: dict[str, Any] = Field(default_factory=dict)
    metric_deltas: dict[str, Any] = Field(default_factory=dict)

    # GitOps + outcome
    gitops_status: str | None = None
    gitops_branch: str | None = None
    gitops_pr_url: str | None = None
    verification_outcome: str | None = None
    verification_reason: str | None = None
    final_outcome: str = ""
    rollback_prepared: bool = False

    @property
    def was_applied(self) -> bool:
        return self.gitops_status in {"PREPARED", "PR_CREATED"}

    @property
    def was_rejected(self) -> bool:
        return self.policy_status == "REJECTED"

    @property
    def needs_review(self) -> bool:
        return self.policy_status == "REQUIRE_REVIEW"

    @classmethod
    def from_lifecycle(cls, lifecycle: Any) -> DecisionRecord:
        """Build a record from an ``agent.lifecycle.OptimizationLifecycle``.

        Accepts the model or a plain dict (``lifecycle.model_dump()``), so the
        caller need not import the lifecycle type.
        """
        lc = lifecycle if isinstance(lifecycle, dict) else lifecycle.model_dump(mode="json")

        rec = lc.get("recommendation_json") or {}
        val = lc.get("validation_json") or {}
        meta = rec.get("metadata") or {}
        env = rec.get("environmental_context") or {}
        op = rec.get("operational_context") or {}

        return cls(
            lifecycle_id=lc["lifecycle_id"],
            started_at=lc["started_at"],
            completed_at=lc.get("completed_at"),
            action=rec.get("action", ""),
            reason=rec.get("reason", ""),
            decision_basis=meta.get("decision_basis", ""),
            confidence=meta.get("confidence"),
            current_replicas=rec.get("current_replicas"),
            recommended_replicas=rec.get("recommended_replicas"),
            policy_status=val.get("status", ""),
            policy_reason=val.get("reason", ""),
            safeguards_triggered=list(val.get("safeguards_triggered", []) or []),
            approved_for_gitops_change=bool(val.get("approved_for_gitops_change", False)),
            carbon_intensity_gco2_kwh=env.get("carbon_intensity_gco2_kwh"),
            carbon_region=env.get("region"),
            renewable_percentage=env.get("renewable_percentage"),
            fossil_fuel_percentage=env.get("fossil_fuel_percentage"),
            carbon_data_available=env.get("data_available"),
            carbon_data_timestamp_seconds=env.get("data_timestamp_seconds"),
            obs_cpu_request_ratio=op.get("cpu_request_ratio"),
            obs_p99_latency_seconds=op.get("p99_latency_seconds"),
            obs_error_rate_rps=op.get("error_rate_rps"),
            obs_availability_ratio=op.get("availability_ratio"),
            pre_snapshot=lc.get("pre_snapshot_json") or {},
            post_snapshot=lc.get("post_snapshot_json") or {},
            metric_deltas=lc.get("metric_deltas_json") or {},
            gitops_status=lc.get("gitops_status"),
            gitops_branch=lc.get("gitops_branch"),
            gitops_pr_url=lc.get("gitops_pr_url"),
            verification_outcome=lc.get("verification_outcome"),
            verification_reason=lc.get("verification_reason"),
            final_outcome=lc.get("final_outcome") or "",
            rollback_prepared=bool(lc.get("rollback_prepared", False)),
        )


class DecisionHistoryStore:
    """Append-only JSONL store of ``DecisionRecord``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def append(self, record: DecisionRecord) -> None:
        """Append one record atomically-ish (single write, file lock held)."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = record.model_dump_json() + "\n"
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _iter_raw(self) -> list[tuple[int, str]]:
        if not self._path.is_file():
            return []
        out: list[tuple[int, str]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                line = line.strip()
                if line:
                    out.append((i, line))
        return out

    def load(self) -> tuple[list[DecisionRecord], int]:
        """Return ``(records, skipped_line_count)``.

        Records are sorted by ``started_at``. ``skipped_line_count`` > 0 means
        the history is known to be incomplete (corrupt lines) and callers
        should surface that.
        """
        records: list[DecisionRecord] = []
        skipped = 0
        for lineno, raw in self._iter_raw():
            try:
                records.append(DecisionRecord.model_validate_json(raw))
            except (ValueError, TypeError) as exc:
                skipped += 1
                log.warning(
                    "chat.history.corrupt_line", path=str(self._path), line=lineno, error=str(exc)
                )
        records.sort(key=lambda r: r.started_at)
        return records, skipped

    def all(self) -> list[DecisionRecord]:
        return self.load()[0]

    def in_range(self, start_ts: float, end_ts: float) -> list[DecisionRecord]:
        """Records whose decision *started* within [start_ts, end_ts)."""
        return [r for r in self.all() if start_ts <= r.started_at < end_ts]

    def get(self, lifecycle_id: str) -> DecisionRecord | None:
        for r in self.all():
            if r.lifecycle_id == lifecycle_id:
                return r
        return None

    def latest(self) -> DecisionRecord | None:
        recs = self.all()
        return recs[-1] if recs else None

    def rewrite(self, records: list[DecisionRecord]) -> None:
        """Replace the whole file (test/maintenance helper)."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".jsonl")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for r in sorted(records, key=lambda x: x.started_at):
                        fh.write(r.model_dump_json() + "\n")
                os.replace(tmp, self._path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


def record_lifecycle(store: DecisionHistoryStore, lifecycle: Any) -> DecisionRecord:
    """Flatten a lifecycle and append it to the store. Returns the record."""
    record = DecisionRecord.from_lifecycle(lifecycle)
    store.append(record)
    return record
