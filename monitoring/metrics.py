"""
monitoring/metrics.py — Prometheus metrics emitted by the GreenOps AI Agent itself.

These metrics are separate from the demo workload metrics (app/metrics.py).
They describe the agent's own health and behaviour — not the workload it manages.

All metrics are prefixed with ``greenops_agent_`` for easy filtering in Grafana.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# ---------------------------------------------------------------------------
# Agent identity
# ---------------------------------------------------------------------------
AGENT_INFO = Info(
    name="greenops_agent",
    documentation="GreenOps AI Agent — version and configuration metadata.",
)

# ---------------------------------------------------------------------------
# Poll cycle metrics
# ---------------------------------------------------------------------------
AGENT_POLL_TOTAL = Counter(
    name="greenops_agent_poll_total",
    documentation="Total number of agent poll cycles executed.",
    labelnames=["status"],  # success | error
)

AGENT_POLL_DURATION_SECONDS = Histogram(
    name="greenops_agent_poll_duration_seconds",
    documentation="Time taken to complete one full agent poll cycle.",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

# ---------------------------------------------------------------------------
# Decision metrics
# ---------------------------------------------------------------------------
AGENT_DECISIONS_TOTAL = Counter(
    name="greenops_agent_decisions_total",
    documentation="Total scaling decisions made by the agent.",
    labelnames=["action"],  # scale_up | scale_down | hold
)

AGENT_REPLICA_TARGET = Gauge(
    name="greenops_agent_replica_target",
    documentation="The replica count in the agent's latest read-only recommendation.",
    labelnames=["namespace", "deployment"],
)

# ---------------------------------------------------------------------------
# Carbon signal tracking
# ---------------------------------------------------------------------------
AGENT_CARBON_INTENSITY = Gauge(
    name="greenops_agent_carbon_intensity_gco2_per_kwh",
    documentation=(
        "Most recent carbon intensity value (gCO2eq/kWh) observed by the agent. "
        "Re-exported here so dashboards have a single source even if the carbon "
        "exporter is temporarily unavailable."
    ),
    labelnames=["zone"],
)

AGENT_CARBON_THRESHOLD_BREACHES = Counter(
    name="greenops_agent_carbon_threshold_breaches_total",
    documentation="Number of times the carbon intensity crossed a threshold.",
    labelnames=["threshold"],  # high | low
)

# ---------------------------------------------------------------------------
# Prometheus collection health
# ---------------------------------------------------------------------------
AGENT_PROMETHEUS_QUERY_ERRORS = Counter(
    name="greenops_agent_prometheus_query_errors_total",
    documentation="Total Prometheus query errors encountered by the agent.",
    labelnames=["metric_name", "error_type"],
)

AGENT_PROMETHEUS_QUERY_DURATION_SECONDS = Histogram(
    name="greenops_agent_prometheus_query_duration_seconds",
    documentation="Latency of individual Prometheus queries made by the agent.",
    labelnames=["metric_name"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

AGENT_OBSERVATION_COMPLETENESS = Gauge(
    name="greenops_agent_observation_completeness_ratio",
    documentation=(
        "Fraction of expected metrics successfully collected in the last poll "
        "(1.0 = all metrics present, <1.0 = partial data)."
    ),
)

# ---------------------------------------------------------------------------
# GitOps operation metrics
# ---------------------------------------------------------------------------
AGENT_GITOPS_COMMITS_TOTAL = Counter(
    name="greenops_agent_gitops_commits_total",
    documentation="Reserved for a future approved control phase; Phase 6 never updates it.",
    labelnames=["status"],  # success | error
)


# ---------------------------------------------------------------------------
# Convenience class for injection / mocking in tests
# ---------------------------------------------------------------------------


class AgentMetrics:
    """
    Facade that groups all agent metric objects.

    Inject this into agent components instead of importing module-level
    metrics directly — makes unit testing (with mock metrics) straightforward.
    """

    info = AGENT_INFO
    poll_total = AGENT_POLL_TOTAL
    poll_duration = AGENT_POLL_DURATION_SECONDS
    decisions_total = AGENT_DECISIONS_TOTAL
    replica_target = AGENT_REPLICA_TARGET
    carbon_intensity = AGENT_CARBON_INTENSITY
    carbon_threshold_breaches = AGENT_CARBON_THRESHOLD_BREACHES
    prometheus_query_errors = AGENT_PROMETHEUS_QUERY_ERRORS
    prometheus_query_duration = AGENT_PROMETHEUS_QUERY_DURATION_SECONDS
    observation_completeness = AGENT_OBSERVATION_COMPLETENESS
    gitops_commits_total = AGENT_GITOPS_COMMITS_TOTAL
