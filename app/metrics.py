"""
app/metrics.py — Prometheus metric definitions for the GreenOps demo workload.

All metrics are registered once at module import time.  Import the metric
objects directly in main.py to update them.

Naming follows the Prometheus best-practice convention:
    <namespace>_<subsystem>_<name>_<unit>
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# ---------------------------------------------------------------------------
# Build / version info
# ---------------------------------------------------------------------------
APP_INFO = Info(
    name="greenops_demo_app",
    documentation="GreenOps demo workload — version and build metadata.",
)

# ---------------------------------------------------------------------------
# HTTP request metrics
# ---------------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    name="greenops_demo_http_requests_total",
    documentation="Total number of HTTP requests received.",
    labelnames=["method", "endpoint", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    name="greenops_demo_http_request_duration_seconds",
    documentation="HTTP request latency in seconds.",
    labelnames=["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ---------------------------------------------------------------------------
# Work-simulation metrics
# ---------------------------------------------------------------------------
WORK_REQUESTS_TOTAL = Counter(
    name="greenops_demo_work_requests_total",
    documentation="Total number of /work requests processed.",
    labelnames=["intensity"],  # low | medium | high
)

WORK_DURATION_SECONDS = Histogram(
    name="greenops_demo_work_duration_seconds",
    documentation="Time spent processing a /work request.",
    labelnames=["intensity"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# ---------------------------------------------------------------------------
# Application health
# ---------------------------------------------------------------------------
APP_READY = Gauge(
    name="greenops_demo_app_ready",
    documentation="1 if the application is ready to serve traffic, 0 otherwise.",
)

# Initialise to ready=1 on startup; the readiness handler updates this.
APP_READY.set(1)
