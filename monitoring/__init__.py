"""
monitoring — Prometheus integration for the GreenOps AI Agent.

Public API
----------
PrometheusClient   HTTP client for querying the Prometheus HTTP API.
GreenOpsQueries    Registry of all named PromQL queries used by the agent.
PrometheusMetrics  Prometheus metrics emitted by the agent itself.

Usage::

    from monitoring import PrometheusClient, GreenOpsQueries

    client = PrometheusClient(base_url="http://prometheus:9090")
    result = await client.instant_query(GreenOpsQueries.cpu_utilization())
"""

from monitoring.client import PrometheusClient, PrometheusError, PrometheusQueryError
from monitoring.queries import GreenOpsQueries
from monitoring.metrics import AgentMetrics

__all__ = [
    "PrometheusClient",
    "PrometheusError",
    "PrometheusQueryError",
    "GreenOpsQueries",
    "AgentMetrics",
]
