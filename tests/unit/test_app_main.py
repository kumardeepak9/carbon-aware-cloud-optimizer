"""
tests/unit/test_app_main.py — Demo workload HTTP endpoint tests.

These tests protect the operational metrics surface consumed by Prometheus and,
indirectly, the GreenOps decision loop.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import APP_READY, app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        APP_READY.set(1)
        yield test_client
        APP_READY.set(1)


def test_health_and_root_endpoints_return_status(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "alive"

    root = client.get("/")
    assert root.status_code == 200
    body = root.json()
    assert body["status"] == "running"
    assert body["endpoints"]["metrics"] == "/metrics"


def test_readiness_reflects_app_ready_gauge(client: TestClient) -> None:
    assert client.get("/ready").status_code == 200

    APP_READY.set(0)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "Not ready"


def test_work_endpoint_records_low_intensity_work(client: TestClient) -> None:
    response = client.post("/work", params={"intensity": "low"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["intensity"] == "low"
    assert body["iterations"] == 5_000
    assert isinstance(body["duration_ms"], float)
    assert len(body["result_digest"]) == 16


def test_work_rejects_invalid_intensity(client: TestClient) -> None:
    response = client.post("/work", params={"intensity": "extreme"})

    assert response.status_code == 422


def test_metrics_endpoint_exposes_operational_metrics(client: TestClient) -> None:
    client.get("/health")
    client.post("/work", params={"intensity": "low"})

    response = client.get("/metrics")

    assert response.status_code == 200
    text = response.text
    assert "greenops_demo_http_requests_total" in text
    assert "greenops_demo_http_request_duration_seconds" in text
    assert 'greenops_demo_work_requests_total{intensity="low"}' in text
    assert "greenops_demo_app_ready" in text
