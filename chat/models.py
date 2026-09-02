"""Typed values exchanged by the GreenOps chat interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class QueryIntent(StrEnum):
    """What a user question is asking for."""

    DECISIONS_IN_RANGE = "decisions_in_range"
    WHY_SCALED_DOWN = "why_scaled_down"
    CARBON_AT_TIME = "carbon_at_time"
    LATENCY_AFTER_OPTIMIZATION = "latency_after_optimization"
    REJECTED_BY_POLICY = "rejected_by_policy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TimeRange:
    """A resolved [start, end) window, in UTC."""

    start: datetime
    end: datetime
    label: str = ""

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeRange bounds must be timezone-aware")
        if self.start > self.end:
            raise ValueError(f"time range start {self.start} is after end {self.end}")

    def contains(self, ts: float) -> bool:
        """True if a unix timestamp falls within [start, end)."""
        return self.start.timestamp() <= ts < self.end.timestamp()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
        }


@dataclass(frozen=True)
class Evidence:
    """A single retrieved fact backing part of an answer.

    Every number an answer states must trace to one of these. `source` names
    the retrieval origin (``decision_history`` / ``prometheus`` / ``carbon_context``),
    `ref` identifies the specific record or series, and `detail` holds the raw
    retrieved values.
    """

    source: str
    ref: str
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "ref": self.ref, "detail": self.detail}
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
            out["time"] = datetime.fromtimestamp(self.timestamp, tz=UTC).isoformat()
        return out


@dataclass(frozen=True)
class GroundedAnswer:
    """The result of a chat query.

    `text` is human-readable prose composed only from retrieved data.
    `evidence` lists every record/series the text draws on.
    `data_complete` is False when a relevant source had no data for the window
    (the text then says so explicitly).
    `unanswered_reason` is set when the interface could not answer at all
    (unknown intent, invalid date range, no data anywhere).
    """

    text: str
    intent: QueryIntent
    evidence: list[Evidence] = field(default_factory=list)
    data_complete: bool = True
    unanswered_reason: str | None = None
    time_range: TimeRange | None = None

    @property
    def answered(self) -> bool:
        return self.unanswered_reason is None

    @property
    def grounded(self) -> bool:
        """An answer is grounded if it either cites evidence or explicitly
        reports the absence of data. It is never grounded to state figures
        with no evidence."""
        return bool(self.evidence) or not self.data_complete or self.unanswered_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "intent": self.intent.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "data_complete": self.data_complete,
            "unanswered_reason": self.unanswered_reason,
            "time_range": self.time_range.to_dict() if self.time_range else None,
        }
