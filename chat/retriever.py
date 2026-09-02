"""Retrieval helpers for the chat interface.

``HistoryRetriever`` reads the decision-history store. ``MetricRetriever`` reads
Prometheus time-series. Both return typed results that make "no data" an
explicit, first-class outcome — never an empty success that a caller might
paper over with a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from chat.history import DecisionHistoryStore, DecisionRecord
from chat.models import TimeRange

try:  # monitoring is a hard dependency, but keep the import failure legible
    from monitoring.client import (
        EmptyResultError,
        PrometheusClient,
        PrometheusError,
    )
    from monitoring.queries import GreenOpsQueries
except ImportError as exc:  # pragma: no cover
    raise ImportError("chat.retriever requires the `monitoring` package") from exc


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistorySlice:
    """Decision records for a window, plus whether the history is trustworthy."""

    records: list[DecisionRecord]
    time_range: TimeRange
    store_exists: bool
    skipped_lines: int

    @property
    def complete(self) -> bool:
        return self.store_exists and self.skipped_lines == 0

    def scale_downs(self) -> list[DecisionRecord]:
        return [r for r in self.records if r.action == "SCALE_DOWN"]

    def scale_ups(self) -> list[DecisionRecord]:
        return [r for r in self.records if r.action == "SCALE_UP"]

    def rejected(self) -> list[DecisionRecord]:
        return [r for r in self.records if r.was_rejected]

    def require_review(self) -> list[DecisionRecord]:
        return [r for r in self.records if r.needs_review]

    def applied(self) -> list[DecisionRecord]:
        return [r for r in self.records if r.was_applied]


class HistoryRetriever:
    """Reads the append-only decision-history store."""

    def __init__(self, store: DecisionHistoryStore) -> None:
        self._store = store

    def slice(self, time_range: TimeRange) -> HistorySlice:
        records, skipped = self._store.load()
        in_window = [
            r for r in records
            if time_range.start.timestamp() <= r.started_at < time_range.end.timestamp()
        ]
        return HistorySlice(
            records=in_window,
            time_range=time_range,
            store_exists=self._store.exists,
            skipped_lines=skipped,
        )

    def latest(self) -> DecisionRecord | None:
        return self._store.latest()

    def get(self, lifecycle_id: str) -> DecisionRecord | None:
        return self._store.get(lifecycle_id)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricWindow:
    """A Prometheus range-query result.

    ``available`` is False whenever Prometheus was unreachable, errored, or
    returned no samples — ``points`` is then empty and ``reason`` explains why.
    No statistic is ever computed from an unavailable window.
    """

    metric: str
    query: str
    available: bool
    points: list[tuple[float, float]] = field(default_factory=list)
    reason: str = ""

    @property
    def first(self) -> tuple[float, float] | None:
        return self.points[0] if self.points else None

    @property
    def last(self) -> tuple[float, float] | None:
        return self.points[-1] if self.points else None

    @property
    def mean(self) -> float | None:
        vals = [v for _, v in self.points if math.isfinite(v)]
        return sum(vals) / len(vals) if vals else None

    @property
    def maximum(self) -> float | None:
        vals = [v for _, v in self.points if math.isfinite(v)]
        return max(vals) if vals else None

    @property
    def minimum(self) -> float | None:
        vals = [v for _, v in self.points if math.isfinite(v)]
        return min(vals) if vals else None

    def value_near(self, ts: float) -> tuple[float, float] | None:
        """The sample closest in time to ``ts`` (within the window)."""
        finite = [(t, v) for t, v in self.points if math.isfinite(v)]
        if not finite:
            return None
        return min(finite, key=lambda p: abs(p[0] - ts))


class MetricRetriever:
    """Reads workload / carbon time-series from Prometheus.

    Pass an *opened* ``PrometheusClient``. Every failure mode collapses to an
    unavailable ``MetricWindow`` — callers must present that as "I don't have
    that metric", never invent a value.
    """

    def __init__(
        self,
        client: PrometheusClient,
        queries: GreenOpsQueries | None = None,
    ) -> None:
        self._client = client
        self._queries = queries or GreenOpsQueries()

    async def _range(
        self, metric: str, expr: str, start: datetime, end: datetime, step: str
    ) -> MetricWindow:
        try:
            resp = await self._client.range_query(expr, start=start, end=end, step=step)
            matrix = resp.as_matrix()
        except EmptyResultError:
            return MetricWindow(metric, expr, available=False, reason="Prometheus returned no samples")
        except PrometheusError as exc:
            return MetricWindow(metric, expr, available=False, reason=f"Prometheus error: {exc}")
        except (ValueError, TypeError) as exc:
            return MetricWindow(metric, expr, available=False, reason=f"unreadable Prometheus response: {exc}")

        points: list[tuple[float, float]] = []
        for series in matrix.result:
            for ts, raw in series.values:
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                points.append((float(ts), val))
        points.sort(key=lambda p: p[0])
        if not points:
            return MetricWindow(metric, expr, available=False, reason="Prometheus returned an empty series")
        return MetricWindow(metric, expr, available=True, points=points)

    async def carbon_intensity(self, time_range: TimeRange, *, step: str = "5m") -> MetricWindow:
        spec = self._queries.carbon_intensity_gco2_kwh()
        return await self._range(
            "carbon_intensity_gco2_kwh", spec.expr, time_range.start, time_range.end, step
        )

    async def p99_latency(self, time_range: TimeRange, *, step: str = "1m") -> MetricWindow:
        spec = self._queries.http_p99_latency_seconds()
        return await self._range(
            "http_p99_latency_seconds", spec.expr, time_range.start, time_range.end, step
        )

    async def around(
        self, metric: str, center_ts: float, *, before_s: float, after_s: float, step: str = "1m"
    ) -> MetricWindow:
        """A window centred on a timestamp, for before/after comparisons."""
        start = datetime.fromtimestamp(center_ts - before_s, tz=UTC)
        end = datetime.fromtimestamp(center_ts + after_s, tz=UTC)
        builder = {
            "http_p99_latency_seconds": self._queries.http_p99_latency_seconds,
            "http_p50_latency_seconds": self._queries.http_p50_latency_seconds,
            "http_error_rate_rps": self._queries.http_error_rate,
            "http_request_rate_rps": self._queries.http_request_rate,
            "carbon_intensity_gco2_kwh": self._queries.carbon_intensity_gco2_kwh,
        }.get(metric)
        if builder is None:
            return MetricWindow(metric, "", available=False, reason=f"no query defined for {metric!r}")
        return await self._range(metric, builder().expr, start, end, step)
