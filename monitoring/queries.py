"""
monitoring/queries.py — Centralised PromQL query registry for GreenOps AI.

All Prometheus queries used by the agent are defined here as static methods on
``GreenOpsQueries``. This single source of truth ensures:

  - Metric names and label matchers stay consistent across agent and tests.
  - Changes to PromQL expressions are reviewed in one place.
  - The AI agent can import pre-validated query strings without building them
    ad-hoc at query time.

Design
------
Each method returns a plain ``str`` (the PromQL expression).
Where appropriate, ``namespace`` and ``deployment`` are parameterised so the
same queries work across dev and prod overlays.

Metric Sources
--------------
  - ``greenops_demo_*``      : Demo workload (app/metrics.py)
  - ``kube_*``               : kube-state-metrics
  - ``container_*``          : cAdvisor (via kubelet)
  - ``node_*``               : node-exporter
  - ``greenops_agent_*``     : Agent self-metrics (monitoring/metrics.py)
  - ``greenops_carbon_*``    : Carbon ingestion layer (carbon/metrics.py)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Query metadata — used by the AI agent to interpret results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuerySpec:
    """Metadata bundle for a single PromQL query."""

    name: str
    """Short machine-readable identifier used in MetricSnapshot.name."""
    expr: str
    """The PromQL expression string."""
    unit: str
    """SI unit of the returned value (e.g. 'cores', 'bytes', 'ratio', 'rps')."""
    description: str
    """Human-readable description of what the metric represents."""
    agent_input: bool = True
    """True if the AI agent uses this as a decision input."""


# ---------------------------------------------------------------------------
# Query registry
# ---------------------------------------------------------------------------


class GreenOpsQueries:
    """
    Registry of all PromQL queries used by the GreenOps AI Agent.

    Usage::

        qs = GreenOpsQueries(namespace="greenops", deployment="greenops-demo-workload")
        expr = qs.cpu_utilization().expr
    """

    def __init__(
        self,
        namespace: str = "greenops",
        deployment: str = "greenops-demo-workload",
        container: str = "workload",
    ) -> None:
        self._ns = namespace
        self._dep = deployment
        # cAdvisor/kubelet expose the *container* name from the Pod spec on the
        # `container` label — for this workload that is `workload`, NOT the
        # Deployment name (see k8s/base/deployment.yaml). Filtering on the wrong
        # value makes every container_* query return an empty result, which the
        # agent's policy treats as missing data and defers on.
        self._container = container

    @property
    def namespace(self) -> str:
        """Namespace targeted by this query registry."""
        return self._ns

    @property
    def deployment(self) -> str:
        """Deployment targeted by this query registry."""
        return self._dep

    @property
    def container(self) -> str:
        """Container name (Pod-spec name) targeted by container_* queries."""
        return self._container

    # ------------------------------------------------------------------
    # CPU
    # ------------------------------------------------------------------

    def cpu_utilization(self) -> QuerySpec:
        """
        CPU cores consumed by the demo workload containers, averaged over 2 min.

        Source: cAdvisor / kubelet.
        """
        return QuerySpec(
            name="cpu_utilization_cores",
            expr=(
                f'sum(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}"'
                f"}}[2m]))"
            ),
            unit="cores",
            description="CPU cores consumed by workload pods (2-min rolling average).",
        )

    def cpu_request_ratio(self) -> QuerySpec:
        """CPU utilization as a fraction of requested CPU (0–N, >1 means throttled)."""
        return QuerySpec(
            name="cpu_request_ratio",
            expr=(
                f'sum(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}"'
                f"}}[2m])) / "
                f'sum(kube_pod_container_resource_requests{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}",'
                f'resource="cpu"'
                f"}})"
            ),
            unit="ratio",
            description="CPU usage / CPU request — above 1.0 means resource pressure.",
        )

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def memory_utilization_bytes(self) -> QuerySpec:
        """Working-set memory consumed by workload pods."""
        return QuerySpec(
            name="memory_utilization_bytes",
            expr=(
                f'sum(container_memory_working_set_bytes{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}"'
                f"}})"
            ),
            unit="bytes",
            description="Total working-set memory used by workload pods.",
        )

    def memory_request_ratio(self) -> QuerySpec:
        """Memory as a fraction of requested memory."""
        return QuerySpec(
            name="memory_request_ratio",
            expr=(
                f'sum(container_memory_working_set_bytes{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}"'
                f"}}) / "
                f'sum(kube_pod_container_resource_requests{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*",'
                f'container="{self._container}",'
                f'resource="memory"'
                f"}})"
            ),
            unit="ratio",
            description="Memory usage / memory request.",
        )

    # ------------------------------------------------------------------
    # Replica count & pod health
    # ------------------------------------------------------------------

    def replica_count_desired(self) -> QuerySpec:
        """Desired replica count from the Deployment spec."""
        return QuerySpec(
            name="replica_count_desired",
            expr=(
                f'kube_deployment_spec_replicas{{'
                f'namespace="{self._ns}",'
                f'deployment="{self._dep}"'
                f"}}"
            ),
            unit="replicas",
            description="Desired replica count set in the Deployment spec.",
        )

    def replica_count_ready(self) -> QuerySpec:
        """Number of ready (passing readiness probe) replicas."""
        return QuerySpec(
            name="replica_count_ready",
            expr=(
                f'kube_deployment_status_replicas_ready{{'
                f'namespace="{self._ns}",'
                f'deployment="{self._dep}"'
                f"}}"
            ),
            unit="replicas",
            description="Replicas currently passing the readiness probe.",
        )

    def pod_availability_ratio(self) -> QuerySpec:
        """Fraction of desired replicas that are ready (1.0 = fully available)."""
        return QuerySpec(
            name="pod_availability_ratio",
            expr=(
                f'kube_deployment_status_replicas_ready{{'
                f'namespace="{self._ns}",'
                f'deployment="{self._dep}"'
                f"}} / "
                f'kube_deployment_spec_replicas{{'
                f'namespace="{self._ns}",'
                f'deployment="{self._dep}"'
                f"}}"
            ),
            unit="ratio",
            description=(
                "Ready replicas / desired replicas. "
                "Agent will not scale down if this is below 1.0."
            ),
        )

    def pod_restart_rate(self) -> QuerySpec:
        """Rate of container restarts — elevated values indicate instability."""
        return QuerySpec(
            name="pod_restart_rate",
            expr=(
                f'sum(rate(kube_pod_container_status_restarts_total{{'
                f'namespace="{self._ns}",'
                f'pod=~"{self._dep}-.*"'
                f"}}[5m]))"
            ),
            unit="restarts/s",
            description="Container restart rate — used to detect pod instability.",
        )

    # ------------------------------------------------------------------
    # HTTP request metrics (from demo workload)
    # ------------------------------------------------------------------

    def http_request_rate(self) -> QuerySpec:
        """Total HTTP request rate across all workload pods.

        ``or vector(0)`` coerces the "no matching series yet" case (fresh pod,
        no requests in the window) to an explicit 0 rps. Without it the query
        returns an empty result, which the agent policy cannot distinguish from
        "metric unavailable" and defers on.

        Note: this counts *every* endpoint, including the Kubernetes liveness/
        readiness probes and Prometheus' own ``/metrics`` scrapes, so a fully
        idle workload still reports a small non-zero rate. See monitoring/README.md.
        """
        return QuerySpec(
            name="http_request_rate_rps",
            expr=(
                f'sum(rate(greenops_demo_http_requests_total{{'
                f'namespace="{self._ns}"'
                f"}}[2m])) or vector(0)"
            ),
            unit="rps",
            description="HTTP requests per second across all workload pods (2-min rate).",
        )

    def http_error_rate(self) -> QuerySpec:
        """Rate of 5xx HTTP errors — used as a safety guard before scaling down.

        ``or vector(0)`` is essential here: on a healthy workload the
        ``status_code=~"5.."`` series never exists, so ``sum(rate(...))`` returns
        an empty result rather than 0. The agent treats a missing required
        signal as a reason to defer, so without the coercion the agent would
        never act on a workload that has simply never returned a 5xx.
        """
        return QuerySpec(
            name="http_error_rate_rps",
            expr=(
                f'sum(rate(greenops_demo_http_requests_total{{'
                f'namespace="{self._ns}",'
                f'status_code=~"5.."'
                f"}}[2m])) or vector(0)"
            ),
            unit="rps",
            description="HTTP 5xx error rate — agent blocks scale-down if elevated.",
        )

    def http_p99_latency_seconds(self) -> QuerySpec:
        """99th-percentile HTTP response latency."""
        return QuerySpec(
            name="http_p99_latency_seconds",
            expr=(
                f'histogram_quantile(0.99, sum(rate('
                f'greenops_demo_http_request_duration_seconds_bucket{{'
                f'namespace="{self._ns}"'
                f"}}[2m])) by (le))"
            ),
            unit="seconds",
            description="P99 HTTP response latency — elevated values block scale-down.",
        )

    def http_p50_latency_seconds(self) -> QuerySpec:
        """Median HTTP response latency."""
        return QuerySpec(
            name="http_p50_latency_seconds",
            expr=(
                f'histogram_quantile(0.50, sum(rate('
                f'greenops_demo_http_request_duration_seconds_bucket{{'
                f'namespace="{self._ns}"'
                f"}}[2m])) by (le))"
            ),
            unit="seconds",
            description="P50 (median) HTTP response latency.",
        )

    # ------------------------------------------------------------------
    # Node utilization
    # ------------------------------------------------------------------

    def node_cpu_utilization(self) -> QuerySpec:
        """Cluster-wide CPU utilization ratio across all nodes."""
        return QuerySpec(
            name="node_cpu_utilization_ratio",
            expr=(
                "1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[2m]))"
            ),
            unit="ratio",
            description=(
                "Fraction of total node CPU in use (0–1). "
                "Used by agent to assess headroom for scale-up."
            ),
        )

    def node_memory_available_bytes(self) -> QuerySpec:
        """Available memory across all cluster nodes."""
        return QuerySpec(
            name="node_memory_available_bytes",
            expr="sum(node_memory_MemAvailable_bytes)",
            unit="bytes",
            description="Total available memory across all nodes.",
        )

    # ------------------------------------------------------------------
    # Carbon signal (from carbon ingestion layer)
    # ------------------------------------------------------------------

    def carbon_intensity_gco2_kwh(self) -> QuerySpec:
        """
        Current grid carbon intensity from the Electricity Maps ingestion layer.

        This is the primary signal driving all GreenOps scaling decisions.
        """
        return QuerySpec(
            name="carbon_intensity_gco2_kwh",
            expr="greenops_carbon_intensity_gco2_per_kwh",
            unit="gCO2eq/kWh",
            description=(
                "Real-time grid carbon intensity from Electricity Maps. "
                "The primary input to all agent scaling decisions."
            ),
        )

    def renewable_percentage(self) -> QuerySpec:
        """Renewable share of electricity generation for the selected grid zone."""
        return QuerySpec(
            name="renewable_percentage",
            expr="greenops_carbon_renewable_percentage",
            unit="percent",
            description="Renewable generation share reported by Electricity Maps.",
        )

    def fossil_fuel_percentage(self) -> QuerySpec:
        """Fossil-fuel share of electricity generation for the selected grid zone."""
        return QuerySpec(
            name="fossil_fuel_percentage",
            expr="greenops_carbon_fossil_fuel_percentage",
            unit="percent",
            description="Fossil-fuel generation share reported by Electricity Maps.",
        )

    def low_carbon_percentage(self) -> QuerySpec:
        """Low-carbon (renewable plus nuclear) grid generation share."""
        return QuerySpec(
            name="low_carbon_percentage",
            expr="greenops_carbon_low_carbon_percentage",
            unit="percent",
            description="Low-carbon generation share reported by Electricity Maps.",
        )

    def carbon_data_available(self) -> QuerySpec:
        """Whether the carbon exporter most recently obtained valid grid data."""
        return QuerySpec(
            name="carbon_data_available",
            expr="greenops_carbon_data_available",
            unit="boolean",
            description="One when the latest carbon exporter collection succeeded.",
        )

    def carbon_last_update_timestamp(self) -> QuerySpec:
        """Timestamp of the grid data point, used to reject stale carbon signals."""
        return QuerySpec(
            name="carbon_last_update_timestamp_seconds",
            expr="greenops_carbon_last_update_timestamp_seconds",
            unit="seconds",
            description="Unix timestamp of the latest Electricity Maps grid data point.",
        )

    # ------------------------------------------------------------------
    # Agent health
    # ------------------------------------------------------------------

    def agent_poll_latency(self) -> QuerySpec:
        """Agent poll cycle duration — used to detect agent performance issues."""
        return QuerySpec(
            name="agent_poll_latency_seconds",
            expr=(
                "histogram_quantile(0.95, sum(rate("
                "greenops_agent_poll_duration_seconds_bucket[5m])) by (le))"
            ),
            unit="seconds",
            description="P95 agent poll cycle latency.",
            agent_input=False,  # internal monitoring, not a decision input
        )

    # ------------------------------------------------------------------
    # Convenience: all agent decision inputs
    # ------------------------------------------------------------------

    def all_decision_inputs(self) -> list[QuerySpec]:
        """
        Return every QuerySpec used by the AI agent as a decision input.

        The agent calls this to build its observation set each poll cycle.
        """
        return [
            qs
            for qs in [
                self.cpu_utilization(),
                self.cpu_request_ratio(),
                self.memory_utilization_bytes(),
                self.memory_request_ratio(),
                self.replica_count_desired(),
                self.replica_count_ready(),
                self.pod_availability_ratio(),
                self.pod_restart_rate(),
                self.http_request_rate(),
                self.http_error_rate(),
                self.http_p99_latency_seconds(),
                self.http_p50_latency_seconds(),
                self.node_cpu_utilization(),
                self.node_memory_available_bytes(),
                self.carbon_intensity_gco2_kwh(),
                self.renewable_percentage(),
                self.fossil_fuel_percentage(),
                self.low_carbon_percentage(),
                self.carbon_data_available(),
                self.carbon_last_update_timestamp(),
            ]
            if qs.agent_input
        ]
