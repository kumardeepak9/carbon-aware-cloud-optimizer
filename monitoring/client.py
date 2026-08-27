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

import time
from datetime import datetime
from typing import Any

import httpx

from config import get_logger
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
    ) -> None:
        """
        Initialise the Prometheus client.

        Args:
            base_url:        Prometheus base URL (no trailing slash).
            timeout_seconds: Per-request timeout; prevents blocking the agent.
            max_retries:     Number of retry attempts on transient errors.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
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

    async def __aenter__(self) -> "PrometheusClient":
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
        try:
            resp = await self._client.get(self._INSTANT_PATH, params=params)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PrometheusConnectionError(
                f"Timeout querying Prometheus ({self._base_url}): {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise PrometheusConnectionError(
                f"HTTP error querying Prometheus: {exc}"
            ) from exc

        duration = time.perf_counter() - t0
        parsed = PrometheusResponse.model_validate(resp.json())

        if not parsed.is_success:
            raise PrometheusQueryError(
                error_type=parsed.error_type or "unknown",
                message=parsed.error or "unknown error",
                query=query,
            )

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
        try:
            resp = await self._client.get(self._RANGE_PATH, params=params)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PrometheusConnectionError(
                f"Timeout on range query ({self._base_url}): {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise PrometheusConnectionError(
                f"HTTP error on range query: {exc}"
            ) from exc

        duration = time.perf_counter() - t0
        parsed = PrometheusResponse.model_validate(resp.json())

        if not parsed.is_success:
            raise PrometheusQueryError(
                error_type=parsed.error_type or "unknown",
                message=parsed.error or "unknown error",
                query=query,
            )

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

        if not vector.result:
            raise EmptyResultError(query=spec.expr)

        snapshots: list[MetricSnapshot] = []
        for sample in vector.result:
            timestamp, value_str = sample.value
            try:
                value = float(value_str)
            except ValueError:
                log.warning(
                    "prometheus.parse.non_numeric",
                    metric=spec.name,
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

        Queries that fail (empty result, connection error) are logged as
        warnings and omitted from the result — partial observations are
        better than a complete failure.

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
            try:
                response = await self.instant_query(spec.expr)
                snapshots = self.parse_vector_to_snapshots(response, spec)
                # Use the first (or only) sample for scalar metrics
                if snapshots:
                    collected_snapshots.append(snapshots[0])
            except EmptyResultError:
                log.warning(
                    "prometheus.collect.no_data",
                    metric=spec.name,
                    query=spec.expr,
                )
            except PrometheusQueryError as exc:
                log.warning(
                    "prometheus.collect.query_error",
                    metric=spec.name,
                    error_type=exc.error_type,
                    message=exc.message,
                )
            except PrometheusConnectionError as exc:
                log.error(
                    "prometheus.collect.connection_error",
                    metric=spec.name,
                    error=str(exc),
                )

        log.info(
            "prometheus.observation_collected",
            total_queries=len(decision_inputs),
            collected=len(collected_snapshots),
            namespace=namespace,
            deployment=deployment,
        )

        return AgentObservation(
            snapshots=collected_snapshots,
            collected_at=time.time(),
            namespace=namespace,
            deployment=deployment,
        )
