"""
monitoring/client.py — Async Prometheus HTTP API client.

Provides a typed, error-handled interface to the Prometheus HTTP API so the
GreenOps AI agent never deals with raw HTTP or JSON parsing.

Features
--------
- Async (httpx) — non-blocking inside the agent event loop.
- Typed returns — all responses are parsed into Pydantic models.
- Configurable timeouts — no hanging queries block the agent loop.
- Structured logging — every query is logged with duration and result count.
- Custom exception hierarchy — callers can distinguish network errors from
  query errors from empty results.

Usage::

    from monitoring.client import PrometheusClient
    from monitoring.queries import GreenOpsQueries

    async with PrometheusClient(base_url="http://prometheus:9090") as client:
        qs = GreenOpsQueries()
        result = await client.instant_query(qs.cpu_utilization().expr)
        snapshots = client.parse_vector_to_snapshots(result, qs.cpu_utilization())
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from config import get_logger
from monitoring.metrics import (
    AGENT_OBSERVATION_COMPLETENESS,
    AGENT_PROMETHEUS_QUERY_DURATION_SECONDS,
    AGENT_PROMETHEUS_QUERY_ERRORS,
)
from monitoring.models import (
    AgentObservation,
    MatrixData,
    MetricSnapshot,
    PrometheusResponse,
    VectorData,
)
from monitoring.queries import GreenOpsQueries, QuerySpec

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class PrometheusError(Exception):
    """Base class for all Prometheus client errors."""


class PrometheusConnectionError(PrometheusError):
    """Raised when the Prometheus server is unreachable."""


class PrometheusQueryError(PrometheusError):
    """
    Raised when Prometheus returns status='error'.

    Attributes
    ----------
    error_type : str   Prometheus errorType field.
    message    : str   Prometheus error field.
    query      : str   The PromQL expression that caused the error.
    """

    def __init__(self, error_type: str, message: str, query: str) -> None:
        self.error_type = error_type
        self.message = message
        self.query = query
        super().__init__(f"[{error_type}] {message} (query={query!r})")


class EmptyResultError(PrometheusError):
    """Raised when a query returns zero samples (no data in range)."""

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__(f"Query returned no data: {query!r}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrometheusClient:
    """
    Async HTTP client for the Prometheus HTTP API.

    Can be used as an async context manager::

        async with PrometheusClient("http://prometheus:9090") as client:
            result = await client.instant_query(expr)

    Or with explicit lifecycle management::

        client = PrometheusClient("http://prometheus:9090")
        await client.open()
        ...
        await client.close()
    """

    _INSTANT_PATH = "/api/v1/query"
    _RANGE_PATH = "/api/v1/query_range"
    _HEALTH_PATH = "/-/healthy"

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.2,
        max_sample_age_seconds: float | None = 300.0,
    ) -> None:
        """
        Initialise the Prometheus client.

        Args:
            base_url:              Prometheus base URL (no trailing slash).
            timeout_seconds:       Per-request timeout; prevents blocking the agent.
            max_retries:           Extra attempts on transient (connection/timeout)
                                   errors. Query errors and empty results are never
                                   retried. 0 disables retrying.
            retry_backoff_seconds: Base delay between retries (doubled each attempt).
            max_sample_age_seconds: Drop Prometheus samples older than this age before
                                    passing them to the decision policy. None disables
                                    sample-age filtering.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max(0, max_retries)
        self._retry_backoff = max(0.0, retry_backoff_seconds)
        self._max_sample_age_seconds = max_sample_age_seconds
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the underlying HTTP connection pool."""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> PrometheusClient:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise PrometheusConnectionError(
                "Client not opened. Use 'async with PrometheusClient(...)' "
                "or call 'await client.open()' first."
            )
        return self._http

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def is_healthy(self) -> bool:
        """
        Check whether the Prometheus server is reachable and healthy.

        Returns:
            True if server responds with HTTP 200, False otherwise.
        """
        try:
            resp = await self._client.get(self._HEALTH_PATH)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # Shared request execution (retry + error-envelope handling)
    # ------------------------------------------------------------------

    async def _execute(
        self,
        path: str,
        params: dict[str, Any],
        query: str,
    ) -> PrometheusResponse:
        """
        Issue one Prometheus API GET, with bounded retries on transient errors.

        Distinguishes the three failure modes the agent cares about:

        - ``PrometheusConnectionError`` — server unreachable / timed out / returned
          an unparseable body. Retried up to ``max_retries`` times.
        - ``PrometheusQueryError`` — server understood the request and rejected it
          (bad PromQL, ``bad_data``, ``unavailable`` …). Prometheus signals this
          with a JSON ``{"status":"error", ...}`` envelope, often alongside an
          HTTP 4xx/5xx status, so the body is inspected *before* the HTTP status
          is treated as a transport failure. Never retried.
        - success — returned to the caller.
        """
        last_exc: PrometheusConnectionError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_exc = PrometheusConnectionError(
                    f"Timeout querying Prometheus ({self._base_url}): {exc}"
                )
            except httpx.HTTPError as exc:
                last_exc = PrometheusConnectionError(
                    f"HTTP error querying Prometheus ({self._base_url}): {exc}"
                )
            else:
                # Got a response. A non-2xx status may still carry a structured
                # Prometheus error envelope — parse the body before deciding.
                try:
                    parsed = self._parse_body(resp, query)
                except PrometheusConnectionError as exc:
                    # Unparseable body (proxy HTML error page, truncated response).
                    # Transient — fall through to the retry path.
                    last_exc = exc
                else:
                    if not parsed.is_success:
                        # Definitive rejection from Prometheus itself. Not retried.
                        raise PrometheusQueryError(
                            error_type=parsed.error_type or "unknown",
                            message=parsed.error or "unknown error",
                            query=query,
                        )
                    if resp.is_error:
                        # HTTP 4xx/5xx but the body claimed success and carried no
                        # error detail — treat as a transport failure and retry.
                        last_exc = PrometheusConnectionError(
                            f"Prometheus returned HTTP {resp.status_code} for {query!r}"
                        )
                    else:
                        return parsed

            if attempt < self._max_retries:
                log.warning(
                    "prometheus.request.retry",
                    query=query,
                    attempt=attempt + 1,
                    error=str(last_exc),
                )
                if self._retry_backoff:
                    await asyncio.sleep(self._retry_backoff * (2**attempt))

        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _parse_body(resp: httpx.Response, query: str) -> PrometheusResponse:
        """Parse an HTTP response body into a PrometheusResponse.

        Raises PrometheusConnectionError if the body is not valid Prometheus
        JSON (e.g. an HTML error page from a reverse proxy).
        """
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PrometheusConnectionError(
                f"Non-JSON response from Prometheus (HTTP {resp.status_code}) "
                f"for {query!r}: {exc}"
            ) from exc
        try:
            return PrometheusResponse.model_validate(payload)
        except ValidationError as exc:
            raise PrometheusConnectionError(
                f"Unrecognised Prometheus response shape for {query!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Instant query (vector)
    # ------------------------------------------------------------------

    async def instant_query(
        self,
        query: str,
        at: datetime | None = None,
    ) -> PrometheusResponse:
        """
        Execute a PromQL instant query.

        Args:
            query: PromQL expression string.
            at:    Point in time (defaults to now).

        Returns:
            Parsed PrometheusResponse — call .as_vector() on it.

        Raises:
            PrometheusConnectionError: Server unreachable.
            PrometheusQueryError:      Server returned status='error'.
        """
        params: dict[str, Any] = {"query": query}
        if at is not None:
            params["time"] = at.timestamp()

        t0 = time.perf_counter()
        parsed = await self._execute(self._INSTANT_PATH, params, query)
        duration = time.perf_counter() - t0

        result_count = len(parsed.data.get("result", [])) if parsed.data else 0
        log.debug(
            "prometheus.instant_query",
            query=query,
            result_count=result_count,
            duration_ms=round(duration * 1000, 2),
        )
        return parsed

    # ------------------------------------------------------------------
    # Range query (matrix)
    # ------------------------------------------------------------------

    async def range_query(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "60s",
    ) -> PrometheusResponse:
        """
        Execute a PromQL range query.

        Args:
            query: PromQL expression string.
            start: Start of the time window.
            end:   End of the time window.
            step:  Resolution step (e.g. '60s', '5m').

        Returns:
            Parsed PrometheusResponse — call .as_matrix() on it.
        """
        params: dict[str, Any] = {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }

        t0 = time.perf_counter()
        parsed = await self._execute(self._RANGE_PATH, params, query)
        duration = time.perf_counter() - t0

        series_count = len(parsed.data.get("result", [])) if parsed.data else 0
        log.debug(
            "prometheus.range_query",
            query=query,
            series_count=series_count,
            step=step,
            duration_ms=round(duration * 1000, 2),
        )
        return parsed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_vector_to_snapshots(
        response: PrometheusResponse,
        spec: QuerySpec,
        *,
        max_sample_age_seconds: float | None = None,
        now: float | None = None,
    ) -> list[MetricSnapshot]:
        """
        Convert a vector (instant query) response into MetricSnapshot objects.

        Args:
            response: A successful PrometheusResponse from instant_query().
            spec:     The QuerySpec that produced the response.

        Returns:
            List of MetricSnapshot — one per time-series in the result.

        Raises:
            EmptyResultError: If the result set is empty.
            ValueError:       If the response is not a vector.
        """
        vector: VectorData = response.as_vector()
        current_time = time.time() if now is None else now

        if not vector.result:
            raise EmptyResultError(query=spec.expr)

        if len(vector.result) > 1:
            # The agent's decision queries are all aggregated to a single series
            # (sum(...), a scalar carbon gauge, one deployment-scoped kube_* series).
            # More than one series means the query is under-aggregated or a stale
            # label-set is lingering (e.g. a pod that was deleted, a changed zone).
            # collect_agent_observation() would silently pick result[0]; surface it.
            log.warning(
                "prometheus.parse.multiple_series",
                metric=spec.name,
                query=spec.expr,
                series_count=len(vector.result),
                label_sets=[s.metric for s in vector.result][:5],
            )

        snapshots: list[MetricSnapshot] = []
        for sample in vector.result:
            timestamp, value_str = sample.value
            if (
                max_sample_age_seconds is not None
                and current_time - timestamp > max_sample_age_seconds
            ):
                log.warning(
                    "prometheus.parse.stale_sample",
                    metric=spec.name,
                    query=spec.expr,
                    sample_age_seconds=round(current_time - timestamp, 3),
                    max_sample_age_seconds=max_sample_age_seconds,
                )
                continue
            try:
                value = float(value_str)
            except (ValueError, TypeError):
                log.warning(
                    "prometheus.parse.non_numeric",
                    metric=spec.name,
                    raw_value=value_str,
                )
                continue

            if not math.isfinite(value):
                # NaN is what histogram_quantile() returns when every bucket's
                # rate is 0 (no traffic in the window); +Inf/-Inf come from
                # divide-by-zero ratios. None are usable decision inputs — drop
                # them here so the value never reaches the policy as a real number.
                log.warning(
                    "prometheus.parse.non_finite",
                    metric=spec.name,
                    query=spec.expr,
                    raw_value=value_str,
                )
                continue

            snapshots.append(
                MetricSnapshot(
                    name=spec.name,
                    query=spec.expr,
                    value=value,
                    labels=sample.metric,
                    timestamp=timestamp,
                    unit=spec.unit,
                )
            )
        return snapshots

    @staticmethod
    def parse_matrix_to_series(response: PrometheusResponse, spec: QuerySpec) -> MatrixData:
        """
        Extract the MatrixData from a range query response.

        Args:
            response: A successful PrometheusResponse from range_query().
            spec:     The QuerySpec that produced the response.

        Returns:
            MatrixData with a list of RangeSample objects.
        """
        return response.as_matrix()

    # ------------------------------------------------------------------
    # High-level: collect all agent decision inputs
    # ------------------------------------------------------------------

    async def collect_agent_observation(
        self,
        queries: GreenOpsQueries,
        namespace: str = "greenops",
        deployment: str = "greenops-demo-workload",
    ) -> AgentObservation:
        """
        Run all agent decision-input queries and return an AgentObservation.

        Queries that fail (empty result, query error, connection error, or a
        malformed response) are logged as warnings and omitted from the result —
        a partial observation is better than a complete failure, and the policy
        layer decides whether the surviving signals are sufficient to act on.

        Side effect: updates ``greenops_agent_observation_completeness_ratio``
        and the per-metric Prometheus query error/latency metrics so the
        ``AgentObservationIncomplete`` alert has data to evaluate.

        Args:
            queries:    GreenOpsQueries instance (parameterised for ns/deployment).
            namespace:  Target Kubernetes namespace.
            deployment: Target Deployment name.

        Returns:
            AgentObservation containing all successfully collected MetricSnapshots.
        """
        decision_inputs = queries.all_decision_inputs()
        collected_snapshots: list[MetricSnapshot] = []

        for spec in decision_inputs:
            q0 = time.perf_counter()
            try:
                response = await self.instant_query(spec.expr)
                snapshots = self.parse_vector_to_snapshots(
                    response,
                    spec,
                    max_sample_age_seconds=self._max_sample_age_seconds,
                )
                # Use the first (or only) sample for scalar metrics.
                # parse_vector_to_snapshots() logs a warning when >1 series.
                if snapshots:
                    collected_snapshots.append(snapshots[0])
            except EmptyResultError:
                log.warning(
                    "prometheus.collect.no_data",
                    metric=spec.name,
                    query=spec.expr,
                )
                AGENT_PROMETHEUS_QUERY_ERRORS.labels(
                    metric_name=spec.name, error_type="empty_result"
                ).inc()
            except PrometheusQueryError as exc:
                log.warning(
                    "prometheus.collect.query_error",
                    metric=spec.name,
                    error_type=exc.error_type,
                    message=exc.message,
                )
                AGENT_PROMETHEUS_QUERY_ERRORS.labels(
                    metric_name=spec.name, error_type=exc.error_type or "query_error"
                ).inc()
            except PrometheusConnectionError as exc:
                log.error(
                    "prometheus.collect.connection_error",
                    metric=spec.name,
                    error=str(exc),
                )
                AGENT_PROMETHEUS_QUERY_ERRORS.labels(
                    metric_name=spec.name, error_type="connection_error"
                ).inc()
            except (ValueError, ValidationError) as exc:
                # as_vector() on a non-vector/absent payload, or a result shape
                # the models reject. One bad response must not abort the sweep.
                log.warning(
                    "prometheus.collect.malformed_response",
                    metric=spec.name,
                    query=spec.expr,
                    error=str(exc),
                )
                AGENT_PROMETHEUS_QUERY_ERRORS.labels(
                    metric_name=spec.name, error_type="malformed_response"
                ).inc()
            finally:
                AGENT_PROMETHEUS_QUERY_DURATION_SECONDS.labels(
                    metric_name=spec.name
                ).observe(time.perf_counter() - q0)

        total = len(decision_inputs)
        completeness = len(collected_snapshots) / total if total else 0.0
        AGENT_OBSERVATION_COMPLETENESS.set(completeness)

        log.info(
            "prometheus.observation_collected",
            total_queries=total,
            collected=len(collected_snapshots),
            completeness_ratio=round(completeness, 3),
            namespace=namespace,
            deployment=deployment,
        )

        return AgentObservation(
            snapshots=collected_snapshots,
            collected_at=time.time(),
            namespace=namespace,
            deployment=deployment,
        )
