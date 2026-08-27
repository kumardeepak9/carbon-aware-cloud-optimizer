"""
tests/conftest.py — Shared pytest fixtures and configuration.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_prometheus_registry():
    """
    Isolate Prometheus metrics between tests.

    prometheus_client uses a global CollectorRegistry by default.
    Without isolation, metric re-registration between test modules causes errors.
    This fixture is a no-op placeholder; individual tests that need metric
    isolation should use prometheus_client.REGISTRY.unregister() or a
    fresh registry.
    """
    yield
