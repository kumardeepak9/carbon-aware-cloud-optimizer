# monitoring

Prometheus integration layer for the GreenOps AI Agent.

## Module Layout

```
monitoring/
├── __init__.py              # Public API: PrometheusClient, GreenOpsQueries, AgentMetrics, CarbonMetrics
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

carbon/                      # Phase 4 — Electricity Maps → Prometheus exporter
├── __init__.py              # Public API: CarbonMetricsExporter, CarbonMetrics, ElectricityMapsData
├── models.py                # Pydantic models for Electricity Maps API responses
├── metrics.py               # Carbon metric objects (greenops_carbon_* series)
└── exporter.py              # CarbonMetricsExporter: fetches data, updates metrics
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
| `renewable_percentage` | `greenops_carbon_renewable_percentage` | percent | carbon/ layer | Grid generation context |
| `fossil_fuel_percentage` | `greenops_carbon_fossil_fuel_percentage` | percent | carbon/ layer | Grid generation context |
| `low_carbon_percentage` | `greenops_carbon_low_carbon_percentage` | percent | carbon/ layer | Grid generation context |
| `carbon_data_available` | `greenops_carbon_data_available` | boolean | carbon/ layer | Data safety guard |
| `carbon_last_update_timestamp_seconds` | `greenops_carbon_last_update_timestamp_seconds` | seconds | carbon/ layer | Stale-data safety guard |

### Scale-down Safety Guards

The agent will **not** reduce replicas if any of these conditions are true:

| Condition | Metric | Threshold |
|---|---|---|
| Pods not fully available | `pod_availability_ratio` | `< 1.0` |
| Elevated error rate | `http_error_rate_rps` | `> 0` (non-zero) |
| High P99 latency | `http_p99_latency_seconds` | `> 1.0s` |
| Pod instability | `pod_restart_rate` | `> 0` |

## Phase 6 read-only recommendations

`agent.DecisionPolicy` is deterministic and is the only component permitted to
choose an action or replica target. Any future LLM integration is restricted to
explaining a completed recommendation through `RecommendationExplainer`; it
cannot modify the decision. The agent has no GitHub, Kubernetes, Argo CD, or
infrastructure write client.

## Phase 7 policy validation

`agent.OptimizationSafetyPolicy` validates every agent recommendation before it
can be considered for a future GitOps change. `GreenOpsDecisionAgent.recommend()`
returns a `ValidatedRecommendation` envelope containing both the recommendation
and one validation result:

| Result | Meaning |
|---|---|
| `APPROVED` | The recommendation satisfies all configured safety safeguards. |
| `REJECTED` | The recommendation must not become an infrastructure change. |
| `REQUIRE_REVIEW` | The recommendation is not automatically approved and needs human review. |

The validation layer checks minimum and maximum replicas, CPU and latency/SLA
thresholds, application health, maximum scale-down percentage, optimization
cooldown, missing metrics, and carbon-data freshness. These safeguards are
configured through `AGENT_*` environment variables such as
`AGENT_MIN_REPLICAS`, `AGENT_MAX_REPLICAS`,
`AGENT_CPU_SAFETY_THRESHOLD`, `AGENT_LATENCY_SLA_THRESHOLD_SECONDS`,
`AGENT_MAX_SCALE_DOWN_PERCENTAGE`, `AGENT_OPTIMIZATION_COOLDOWN_SECONDS`, and
`AGENT_MAX_CARBON_DATA_AGE_SECONDS`.

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
pytest tests/unit/test_prometheus_client.py tests/unit/test_carbon_metrics.py -v
```

## Prometheus Rules

Recording rules in `rules/greenops_recording.yml` pre-compute the expensive ratio queries.
The AI agent queries the recording rule series (`greenops:workload:*`) for low-latency reads.

Alert rules in `rules/greenops_alerts.yml` fire when the agent safety guards are violated.

---

## Carbon Metrics (Phase 4)

The `carbon/` package implements the Electricity Maps → Prometheus export pipeline.
Metrics are scraped by Prometheus from port **8002** (job `greenops-carbon-exporter`).

### Exported series

| Metric | Type | Labels | Description |
|---|---|---|---|
| `greenops_carbon_intensity_gco2_per_kwh` | Gauge | `zone` | Grid carbon intensity (gCO2eq/kWh) |
| `greenops_carbon_renewable_percentage` | Gauge | `zone` | % generation from renewables (0–100) |
| `greenops_carbon_fossil_fuel_percentage` | Gauge | `zone` | % generation from fossil fuels (0–100) |
| `greenops_carbon_low_carbon_percentage` | Gauge | `zone` | % low-carbon (renewable + nuclear, 0–100) |
| `greenops_carbon_last_update_timestamp_seconds` | Gauge | `zone` | Unix epoch of the Electricity Maps data point |
| `greenops_carbon_data_available` | Gauge | `zone` | 1 = fresh data, 0 = fetch failed |
| `greenops_carbon_scrape_errors_total` | Counter | `zone`, `error_type` | Cumulative fetch/parse failures |
| `greenops_carbon_scrape_duration_seconds` | Histogram | `zone` | API fetch latency |

`error_type` values: `connection`, `http`, `parse`, `timeout`.

### Label cardinality

Only `zone` is used as a label.  Zone values come from the operator-controlled
`ELECTRICITY_MAPS_ZONE` environment variable — the set of values is bounded and
known at deploy time.  No user-supplied or unbounded values are ever used.

### Safe unavailability

When the Electricity Maps API is unreachable or returns an error:
- `greenops_carbon_data_available` is set to `0`
- `greenops_carbon_scrape_errors_total` is incremented
- All other Gauges **retain their last-set value** (Prometheus carries them forward)
- `update()` never raises — the scheduler / agent loop is not interrupted

When `/power-breakdown/latest` returns 4xx (e.g. restricted API tier):
- Carbon intensity, timestamp, and `data_available` are still exported normally
- Renewable/fossil/low-carbon Gauges retain their prior values
- The error counter is **not** incremented (partial data is expected, not an error)

### Usage

```python
from carbon import CarbonMetricsExporter

exporter = CarbonMetricsExporter(
    api_key=settings.electricity_maps.api_key,  # from ELECTRICITY_MAPS_API_KEY
    zone=settings.electricity_maps.zone,        # from ELECTRICITY_MAPS_ZONE
)
await exporter.open()
# On each poll interval:
data = await exporter.update()
if data:
    print(f"Carbon intensity: {data.carbon_intensity_gco2_per_kwh} gCO2eq/kWh")
await exporter.close()
```
