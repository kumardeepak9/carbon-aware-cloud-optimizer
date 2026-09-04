"""
tests/unit/test_prometheus_parsing.py

Focused tests for Prometheus response parsing and missing-data behaviour:

- non-finite (NaN / +Inf) sample values are dropped, not passed through
- under-aggregated (multi-series) results are surfaced, not silently reduced
- Prometheus error envelopes on HTTP 4xx/5xx are classified as query errors,
  not connection errors
- transient connection failures are retried, hard failures are not
- a malformed response for one metric does not abort the whole observation
- the observation-completeness gauge reflects how much data was collected
- the query registry stays in sync with the policy's required-signal list

All HTTP is mocked with respx; no Prometheus server is required.
"""

from __future__ import annotations

import math
import time

import httpx
import pytest
import respx
from httpx import Response
from prometheus_client import REGISTRY

from agent.policy import DecisionPolicy
from monitoring.client import (
    PrometheusClient,
    PrometheusConnectionError,
    PrometheusQueryError,
)
from monitoring.models import PrometheusResponse
from monitoring.queries import GreenOpsQueries, QuerySpec

PROM = "http://prom-test:9090"
QUERY_URL = f"{PROM}/api/v1/query"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vector(*samples: tuple[dict, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": metric, "value": [time.time(), value]} for metric, value in samples
            ],
        },
    }


def _spec(expr: str = "up") -> QuerySpec:
    return QuerySpec(name="test_metric", expr=expr, unit="ratio", description="t")


# ---------------------------------------------------------------------------
# Non-finite values
# ---------------------------------------------------------------------------


class TestNonFiniteValues:
    def test_nan_sample_is_dropped(self) -> None:
        resp = PrometheusResponse.model_validate(
            _vector(({"le": "+Inf"}, "NaN"), ({"pod": "b"}, "0.5"))
        )
        snaps = PrometheusClient.parse_vector_to_snapshots(resp, _spec())
        assert [s.value for s in snaps] == [0.5]

    def test_positive_infinity_is_dropped(self) -> None:
        resp = PrometheusResponse.model_validate(_vector(({}, "+Inf")))
        snaps = PrometheusClient.parse_vector_to_snapshots(resp, _spec())
        assert snaps == []

    def test_all_finite_values_are_kept(self) -> None:
        resp = PrometheusResponse.model_validate(
            _vector(({"pod": "a"}, "1.0"), ({"pod": "b"}, "2.5"))
        )
        snaps = PrometheusClient.parse_vector_to_snapshots(resp, _spec())
        assert sorted(s.value for s in snaps) == [1.0, 2.5]
        assert all(math.isfinite(s.value) for s in snaps)


# ---------------------------------------------------------------------------
# Multi-series (duplicate / under-aggregated) results
# ---------------------------------------------------------------------------


class TestMultiSeriesResults:
    def test_multiple_series_are_all_returned(self, caplog) -> None:
        resp = PrometheusResponse.model_validate(
            _vector(({"zone": "DE"}, "1.0"), ({"zone": "FR"}, "2.0"))
        )
        snaps = PrometheusClient.parse_vector_to_snapshots(
            resp, _spec("greenops_carbon_data_available")
        )
        assert len(snaps) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_collect_uses_first_series_when_ambiguous(self) -> None:
        # Every query returns two series; collect_agent_observation keeps [0].
        respx.get(QUERY_URL).mock(
            return_value=Response(
                200, json=_vector(({"zone": "DE"}, "7.0"), ({"zone": "FR"}, "9.0"))
            )
        )
        qs = GreenOpsQueries()
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            obs = await client.collect_agent_observation(qs)
        assert obs.snapshots
        assert all(s.value == 7.0 for s in obs.snapshots)


# ---------------------------------------------------------------------------
# HTTP error status + Prometheus error envelope
# ---------------------------------------------------------------------------


class TestErrorEnvelopeClassification:
    @pytest.mark.asyncio
    @respx.mock
    async def test_http_400_with_error_envelope_is_query_error(self) -> None:
        respx.get(QUERY_URL).mock(
            return_value=Response(
                400,
                json={
                    "status": "error",
                    "errorType": "bad_data",
                    "error": "parse error",
                    "data": None,
                },
            )
        )
        async with PrometheusClient(base_url=PROM, max_retries=1) as client:
            with pytest.raises(PrometheusQueryError) as ei:
                await client.instant_query("sum(rate(")
        assert ei.value.error_type == "bad_data"

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_503_with_error_envelope_is_query_error(self) -> None:
        respx.get(QUERY_URL).mock(
            return_value=Response(
                503,
                json={
                    "status": "error",
                    "errorType": "unavailable",
                    "error": "no ingesters",
                    "data": None,
                },
            )
        )
        async with PrometheusClient(base_url=PROM, max_retries=2) as client:
            with pytest.raises(PrometheusQueryError) as ei:
                await client.instant_query("up")
        assert ei.value.error_type == "unavailable"

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_json_body_is_connection_error(self) -> None:
        respx.get(QUERY_URL).mock(return_value=Response(502, text="<html>bad gateway</html>"))
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            with pytest.raises(PrometheusConnectionError):
                await client.instant_query("up")


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


class TestRetries:
    @pytest.mark.asyncio
    @respx.mock
    async def test_transient_connection_error_is_retried_then_succeeds(self) -> None:
        route = respx.get(QUERY_URL).mock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                Response(200, json=_vector(({}, "3.0"))),
            ]
        )
        async with PrometheusClient(
            base_url=PROM, max_retries=2, retry_backoff_seconds=0.0
        ) as client:
            resp = await client.instant_query("up")
        assert route.call_count == 2
        assert float(resp.as_vector().result[0].value[1]) == 3.0

    @pytest.mark.asyncio
    @respx.mock
    async def test_retries_are_bounded_then_raise(self) -> None:
        route = respx.get(QUERY_URL).mock(side_effect=httpx.ConnectError("down"))
        async with PrometheusClient(
            base_url=PROM, max_retries=2, retry_backoff_seconds=0.0
        ) as client:
            with pytest.raises(PrometheusConnectionError):
                await client.instant_query("up")
        assert route.call_count == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    @respx.mock
    async def test_query_error_is_not_retried(self) -> None:
        route = respx.get(QUERY_URL).mock(
            return_value=Response(
                200, json={"status": "error", "errorType": "bad_data", "error": "x", "data": None}
            )
        )
        async with PrometheusClient(
            base_url=PROM, max_retries=3, retry_backoff_seconds=0.0
        ) as client:
            with pytest.raises(PrometheusQueryError):
                await client.instant_query("bad")
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Missing-data behaviour in collect_agent_observation
# ---------------------------------------------------------------------------


class TestObservationMissingData:
    @pytest.mark.asyncio
    @respx.mock
    async def test_malformed_response_does_not_abort_sweep(self) -> None:
        # resultType 'matrix' is invalid for an instant query -> every parse fails,
        # but collect_agent_observation must still return (empty) rather than raise.
        respx.get(QUERY_URL).mock(
            return_value=Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
            )
        )
        qs = GreenOpsQueries()
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            obs = await client.collect_agent_observation(qs)
        assert obs.snapshots == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_completeness_gauge_full(self) -> None:
        respx.get(QUERY_URL).mock(return_value=Response(200, json=_vector(({}, "1.0"))))
        qs = GreenOpsQueries()
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            await client.collect_agent_observation(qs)
        val = REGISTRY.get_sample_value("greenops_agent_observation_completeness_ratio")
        assert val == pytest.approx(1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_completeness_gauge_zero_when_all_empty(self) -> None:
        respx.get(QUERY_URL).mock(
            return_value=Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            )
        )
        qs = GreenOpsQueries()
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            obs = await client.collect_agent_observation(qs)
        assert obs.snapshots == []
        val = REGISTRY.get_sample_value("greenops_agent_observation_completeness_ratio")
        assert val == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Query-string correctness
# ---------------------------------------------------------------------------


class TestQueryCorrectness:
    def test_container_label_matches_pod_spec_name_not_deployment(self) -> None:
        qs = GreenOpsQueries()
        for method in (
            "cpu_utilization",
            "cpu_request_ratio",
            "memory_utilization_bytes",
            "memory_request_ratio",
        ):
            expr = getattr(qs, method)().expr
            assert 'container="workload"' in expr, method
            assert 'container="greenops-demo-workload"' not in expr, method

    def test_container_label_is_configurable(self) -> None:
        qs = GreenOpsQueries(container="app")
        assert 'container="app"' in qs.cpu_utilization().expr
        assert 'container="app"' in qs.cpu_request_ratio().expr
        assert 'container="app"' in qs.memory_utilization_bytes().expr
        assert 'container="app"' in qs.memory_request_ratio().expr

    def test_error_and_request_rate_coerce_empty_to_zero(self) -> None:
        qs = GreenOpsQueries()
        assert qs.http_error_rate().expr.rstrip().endswith("or vector(0)")
        assert qs.http_request_rate().expr.rstrip().endswith("or vector(0)")

    def test_histogram_queries_aggregate_by_le(self) -> None:
        qs = GreenOpsQueries()
        for method in (
            "http_p99_latency_seconds",
            "http_p50_latency_seconds",
            "agent_poll_latency",
        ):
            assert "by (le)" in getattr(qs, method)().expr, method


# ---------------------------------------------------------------------------
# Registry <-> policy contract
# ---------------------------------------------------------------------------


def test_every_required_policy_signal_has_a_query() -> None:
    produced = {spec.name for spec in GreenOpsQueries().all_decision_inputs()}
    required = DecisionPolicy._REQUIRED_SIGNALS
    missing = required - produced
    assert not missing, f"policy requires signals with no QuerySpec: {sorted(missing)}"
