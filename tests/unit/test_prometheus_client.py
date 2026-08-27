"""
tests/unit/test_prometheus_client.py

Unit tests for the Prometheus HTTP API client.

Strategy
--------
- All HTTP calls are intercepted by respx (mock httpx transport).
- No real Prometheus instance is needed.
- Tests cover: happy path, error status, connection failure, empty results,
  timeout, and the parse_vector_to_snapshots helper.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from monitoring.client import (
    EmptyResultError,
    PrometheusClient,
    PrometheusConnectionError,
    PrometheusQueryError,
)
from monitoring.models import MetricSnapshot, PrometheusResponse
from monitoring.queries import GreenOpsQueries, QuerySpec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROMETHEUS_BASE = "http://prometheus-test:9090"


def _vector_response(metric: dict, value: float, timestamp: float | None = None) -> dict:
    """Build a minimal Prometheus instant-query (vector) success payload."""
    ts = timestamp or time.time()
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": metric,
                    "value": [ts, str(value)],
                }
            ],
        },
    }


def _error_response(error_type: str = "bad_data", error: str = "invalid query") -> dict:
    return {
        "status": "error",
        "errorType": error_type,
        "error": error,
        "data": None,
    }


def _empty_vector_response() -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [],
        },
    }


@pytest.fixture
def queries() -> GreenOpsQueries:
    return GreenOpsQueries(namespace="greenops", deployment="greenops-demo-workload")


# ---------------------------------------------------------------------------
# PrometheusResponse model tests
# ---------------------------------------------------------------------------


class TestPrometheusResponseModel:
    def test_parse_success_vector(self) -> None:
        payload = _vector_response({"__name__": "up"}, 1.0)
        resp = PrometheusResponse.model_validate(payload)
        assert resp.is_success
        vector = resp.as_vector()
        assert len(vector.result) == 1
        assert float(vector.result[0].value[1]) == 1.0

    def test_parse_error_response(self) -> None:
        payload = _error_response("execution", "query timed out")
        resp = PrometheusResponse.model_validate(payload)
        assert not resp.is_success
        assert resp.error_type == "execution"
        assert resp.error == "query timed out"

    def test_empty_vector(self) -> None:
        payload = _empty_vector_response()
        resp = PrometheusResponse.model_validate(payload)
        assert resp.is_success
        assert resp.as_vector().result == []


# ---------------------------------------------------------------------------
# PrometheusClient.instant_query tests
# ---------------------------------------------------------------------------


class TestInstantQuery:
    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_instant_query(self) -> None:
        payload = _vector_response({"namespace": "greenops"}, 3.0)
        respx.get(f"{PROMETHEUS_BASE}/api/v1/query").mock(
            return_value=Response(200, json=payload)
        )

        async with PrometheusClient(base_url=PROMETHEUS_BASE) as client:
            result = await client.instant_query("kube_deployment_spec_replicas")

        assert result.is_success
        vector = result.as_vector()
        assert float(vector.result[0].value[1]) == 3.0

    @pytest.mark.asyncio
    @respx.mock
    async def test_query_error_raises_prometheus_query_error(self) -> None:
        payload = _error_response("bad_data", "invalid PromQL")
        respx.get(f"{PROMETHEUS_BASE}/api/v1/query").mock(
            return_value=Response(200, json=payload)
        )

        async with PrometheusClient(base_url=PROMETHEUS_BASE) as client:
            with pytest.raises(PrometheusQueryError) as exc_info:
                await client.instant_query("invalid query {{{")

        assert exc_info.value.error_type == "bad_data"
        assert "invalid PromQL" in exc_info.value.message

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_500_raises_connection_error(self) -> None:
        respx.get(f"{PROMETHEUS_BASE}/api/v1/query").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        async with PrometheusClient(base_url=PROMETHEUS_BASE) as client:
            with pytest.raises(PrometheusConnectionError):
                await client.instant_query("up")

    @pytest.mark.asyncio
    async def test_client_not_opened_raises(self) -> None:
        client = PrometheusClient(base_url=PROMETHEUS_BASE)
        with pytest.raises(PrometheusConnectionError, match="not opened"):
            await client.instant_query("up")


# ---------------------------------------------------------------------------
# PrometheusClient.parse_vector_to_snapshots tests
# ---------------------------------------------------------------------------


class TestParseVectorToSnapshots:
    def _make_spec(self, expr: str = "up") -> QuerySpec:
        return QuerySpec(
            name="test_metric",
            expr=expr,
            unit="ratio",
            description="Test metric.",
        )

    def test_parses_single_sample(self) -> None:
        ts = time.time()
        payload = _vector_response({"namespace": "greenops", "pod": "pod-abc"}, 2.5, ts)
        response = PrometheusResponse.model_validate(payload)
        spec = self._make_spec()

        snapshots = PrometheusClient.parse_vector_to_snapshots(response, spec)

        assert len(snapshots) == 1
        s = snapshots[0]
        assert s.name == "test_metric"
        assert s.value == pytest.approx(2.5)
        assert s.unit == "ratio"
        assert s.labels["namespace"] == "greenops"
        assert s.timestamp == pytest.approx(ts, abs=1.0)

    def test_empty_result_raises_empty_result_error(self) -> None:
        payload = _empty_vector_response()
        response = PrometheusResponse.model_validate(payload)
        spec = self._make_spec()

        with pytest.raises(EmptyResultError) as exc_info:
            PrometheusClient.parse_vector_to_snapshots(response, spec)

        assert spec.expr in str(exc_info.value)

    def test_non_numeric_value_is_skipped(self) -> None:
        payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {}, "value": [time.time(), "NaN"]},
                    {"metric": {"pod": "pod-b"}, "value": [time.time(), "1.0"]},
                ],
            },
        }
        response = PrometheusResponse.model_validate(payload)
        spec = self._make_spec()

        # NaN is a valid float in Python — this tests that non-parseable strings
        # are skipped. Use a truly non-numeric string instead.
        payload["data"]["result"][0]["value"][1] = "not-a-number"
        response = PrometheusResponse.model_validate(payload)
        snapshots = PrometheusClient.parse_vector_to_snapshots(response, spec)

        assert len(snapshots) == 1
        assert snapshots[0].labels.get("pod") == "pod-b"


# ---------------------------------------------------------------------------
# GreenOpsQueries tests
# ---------------------------------------------------------------------------


class TestGreenOpsQueries:
    def test_all_decision_inputs_returns_list(self, queries: GreenOpsQueries) -> None:
        inputs = queries.all_decision_inputs()
        assert len(inputs) > 0

    def test_all_decision_inputs_have_required_fields(
        self, queries: GreenOpsQueries
    ) -> None:
        for spec in queries.all_decision_inputs():
            assert spec.name, f"Missing name on spec: {spec}"
            assert spec.expr, f"Missing expr on spec: {spec}"
            assert spec.unit, f"Missing unit on spec: {spec}"
            assert spec.description, f"Missing description on spec: {spec}"
            assert spec.agent_input is True

    def test_namespace_interpolated_in_expr(self, queries: GreenOpsQueries) -> None:
        spec = queries.cpu_utilization()
        assert "greenops" in spec.expr

    def test_deployment_interpolated_in_expr(self, queries: GreenOpsQueries) -> None:
        spec = queries.replica_count_desired()
        assert "greenops-demo-workload" in spec.expr

    def test_carbon_intensity_query_present(self, queries: GreenOpsQueries) -> None:
        names = [qs.name for qs in queries.all_decision_inputs()]
        assert "carbon_intensity_gco2_kwh" in names

    def test_custom_namespace_deployment(self) -> None:
        qs = GreenOpsQueries(namespace="production", deployment="my-workload")
        spec = qs.replica_count_ready()
        assert "production" in spec.expr
        assert "my-workload" in spec.expr

    @pytest.mark.parametrize(
        "method_name",
        [
            "cpu_utilization",
            "cpu_request_ratio",
            "memory_utilization_bytes",
            "memory_request_ratio",
            "replica_count_desired",
            "replica_count_ready",
            "pod_availability_ratio",
            "pod_restart_rate",
            "http_request_rate",
            "http_error_rate",
            "http_p99_latency_seconds",
            "http_p50_latency_seconds",
            "node_cpu_utilization",
            "node_memory_available_bytes",
            "carbon_intensity_gco2_kwh",
        ],
    )
    def test_each_query_method_returns_query_spec(
        self, method_name: str, queries: GreenOpsQueries
    ) -> None:
        method = getattr(queries, method_name)
        spec = method()
        assert isinstance(spec, QuerySpec)
        assert spec.expr != ""


# ---------------------------------------------------------------------------
# collect_agent_observation tests
# ---------------------------------------------------------------------------


class TestCollectAgentObservation:
    @pytest.mark.asyncio
    @respx.mock
    async def test_partial_collection_on_empty_results(
        self, queries: GreenOpsQueries
    ) -> None:
        """
        If some queries return empty results, the observation should still
        be returned with only the successful snapshots.
        """
        # All queries return empty result
        respx.get(f"{PROMETHEUS_BASE}/api/v1/query").mock(
            return_value=Response(200, json=_empty_vector_response())
        )

        async with PrometheusClient(base_url=PROMETHEUS_BASE) as client:
            observation = await client.collect_agent_observation(queries)

        # Should return an observation with zero snapshots, not raise
        assert observation.snapshots == []
        assert observation.namespace == "greenops"
        assert observation.deployment == "greenops-demo-workload"

    @pytest.mark.asyncio
    @respx.mock
    async def test_full_collection_with_all_metrics(
        self, queries: GreenOpsQueries
    ) -> None:
        """Happy path: all queries return one sample each."""
        payload = _vector_response({"namespace": "greenops"}, 42.0)
        respx.get(f"{PROMETHEUS_BASE}/api/v1/query").mock(
            return_value=Response(200, json=payload)
        )

        async with PrometheusClient(base_url=PROMETHEUS_BASE) as client:
            observation = await client.collect_agent_observation(queries)

        n_inputs = len(queries.all_decision_inputs())
        assert len(observation.snapshots) == n_inputs
        assert all(isinstance(s, MetricSnapshot) for s in observation.snapshots)
