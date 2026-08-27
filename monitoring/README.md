# monitoring

Prometheus integration layer for the GreenOps AI Agent.

## Module Layout

```
monitoring/
├── __init__.py              # Public API: PrometheusClient, GreenOpsQueries, AgentMetrics
├── client.py                # Async Prometheus HTTP API client
├── queries.py               # Centralised PromQL query registry (QuerySpec objects)
├── models.py                # Pydantic models for Prometheus API responses
├── metrics.py               # Agent self-metrics (greenops_agent_* series)
├── prometheus.yml           # Prometheus scrape configuration (mounted in Docker)
├── rules/
│   ├── greenops_recording.yml   # Pre-computed recording rules
│   └── greenops_alerts.yml      # Alerting rules
└── dashboards/
    ├── greenops_workload.json    # Grafana: Workload CPU, memory, replicas, latency
    └── greenops_carbon_agent.json  # Grafana: Carbon intensity vs replica decisions
```

---

## GreenOps AI Agent — Prometheus Metric Inputs

The following metrics are queried every agent poll cycle via `GreenOpsQueries.all_decision_inputs()`.  
They form the **complete observation state** the AI agent uses to make scaling decisions.

| Metric Name | PromQL Expression | Unit | Source | Agent Use |
|---|---|---|---|---|
| `cpu_utilization_cores` | `sum(rate(container_cpu_usage_seconds_total{...}[2m]))` | cores | cAdvisor | Scale signal |
| `cpu_request_ratio` | CPU usage / CPU request | ratio | cAdvisor + kube-state | Pressure detection |
| `memory_utilization_bytes` | `sum(container_memory_working_set_bytes{...})` | bytes | cAdvisor | Scale signal |
| `memory_request_ratio` | Memory usage / Memory request | ratio | cAdvisor + kube-state | Pressure detection |
| `replica_count_desired` | `kube_deployment_spec_replicas{...}` | replicas | kube-state-metrics | Current target |
| `replica_count_ready` | `kube_deployment_status_replicas_ready{...}` | replicas | kube-state-metrics | Safety guard |
| `pod_availability_ratio` | ready replicas / desired replicas | ratio | kube-state-metrics | **Scale-down block** (< 1.0) |
| `pod_restart_rate` | `rate(kube_pod_container_status_restarts_total[5m])` | restarts/s | kube-state-metrics | Instability detection |
| `http_request_rate_rps` | `sum(rate(greenops_demo_http_requests_total[2m]))` | rps | App (app/metrics.py) | Demand signal |
| `http_error_rate_rps` | `sum(rate(...{status_code=~"5.."}[2m]))` | rps | App | **Scale-down block** |
| `http_p99_latency_seconds` | `histogram_quantile(0.99, ...)` | seconds | App | **Scale-down block** |
| `http_p50_latency_seconds` | `histogram_quantile(0.50, ...)` | seconds | App | Baseline latency |
| `node_cpu_utilization_ratio` | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m]))` | ratio | node-exporter | Headroom check |
| `node_memory_available_bytes` | `sum(node_memory_MemAvailable_bytes)` | bytes | node-exporter | Headroom check |
| `carbon_intensity_gco2_kwh` | `greenops_carbon_intensity_gco2_per_kwh` | gCO2eq/kWh | carbon/ layer | **Primary decision driver** |

### Scale-down Safety Guards

The agent will **not** reduce replicas if any of these conditions are true:

| Condition | Metric | Threshold |
|---|---|---|
| Pods not fully available | `pod_availability_ratio` | `< 1.0` |
| Elevated error rate | `http_error_rate_rps` | `> 0` (non-zero) |
| High P99 latency | `http_p99_latency_seconds` | `> 1.0s` |
| Pod instability | `pod_restart_rate` | `> 0` |

---

## Usage

```python
from monitoring import PrometheusClient, GreenOpsQueries

async with PrometheusClient(base_url="http://prometheus:9090") as client:
    queries = GreenOpsQueries(namespace="greenops", deployment="greenops-demo-workload")

    # Collect the full agent observation in one call
    observation = await client.collect_agent_observation(queries)

    for snapshot in observation.snapshots:
        print(f"{snapshot.name}: {snapshot.value} {snapshot.unit}")
```

## Running Tests

```bash
make test-unit
# or directly:
pytest tests/unit/test_prometheus_client.py -v
```

## Prometheus Rules

Recording rules in `rules/greenops_recording.yml` pre-compute the expensive ratio queries.
The AI agent queries the recording rule series (`greenops:workload:*`) for low-latency reads.

Alert rules in `rules/greenops_alerts.yml` fire when the agent safety guards are violated.
