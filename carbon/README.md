# carbon

Electricity Maps carbon data ingestion and Prometheus metric export for GreenOps AI.

## Responsibilities

The `carbon/` package is the bridge between the live electricity grid and Prometheus:

```
Electricity Maps API
      │
      │  GET /carbon-intensity/latest
      │  GET /power-breakdown/latest
      ▼
CarbonMetricsExporter  (carbon/exporter.py)
      │
      │  updates Prometheus gauges/counters
      ▼
CarbonMetrics registry  (carbon/metrics.py)
      │
      │  HTTP pull on port 8002
      ▼
Prometheus  →  Grafana  →  GreenOps AI Agent
```

## Module Layout

```
carbon/
├── __init__.py     # Public API: CarbonMetricsExporter, CarbonMetrics, ElectricityMapsData
├── models.py       # Pydantic models for Electricity Maps API responses
├── metrics.py      # Prometheus metric objects (greenops_carbon_* series)
├── exporter.py     # CarbonMetricsExporter — fetch + parse + update metrics
└── server.py       # CarbonMetricsServer — HTTP exposition server (port 8002)
```

## Exported Prometheus Metrics

All metrics use the `greenops_carbon_` prefix and a single `zone` label.

| Metric | Type | Description |
|---|---|---|
| `greenops_carbon_intensity_gco2_per_kwh` | Gauge | Grid carbon intensity (gCO2eq/kWh) — **primary AI agent signal** |
| `greenops_carbon_renewable_percentage` | Gauge | % generation from renewables (0–100) |
| `greenops_carbon_fossil_fuel_percentage` | Gauge | % generation from fossil fuels (0–100) |
| `greenops_carbon_low_carbon_percentage` | Gauge | % from low-carbon sources (renewable + nuclear, 0–100) |
| `greenops_carbon_last_update_timestamp_seconds` | Gauge | Unix epoch of the Electricity Maps data point |
| `greenops_carbon_data_available` | Gauge | 1 = fresh data, 0 = fetch failed |
| `greenops_carbon_scrape_errors_total` | Counter | Cumulative fetch/parse failures (labelled by `error_type`) |
| `greenops_carbon_scrape_duration_seconds` | Histogram | API fetch cycle latency |

### Label design

Only `zone` is used as a label. Zone values are set by the operator via
`ELECTRICITY_MAPS_ZONE` — the cardinality is bounded and known at deploy time.
No user-supplied, request-derived, or unbounded values are ever used as labels.

## HTTP Exposition

Carbon metrics are served on a **dedicated port (8002)** with an isolated
`CollectorRegistry` — independent of the agent's metrics (port 8001) and the
workload app's metrics (port 8000):

| Port | Owner | Metrics |
|---|---|---|
| 8000 | Demo workload (`app/`) | `greenops_demo_*` |
| 8001 | AI Agent (`agent/`) | `greenops_agent_*` |
| **8002** | **Carbon exporter (`carbon/`)** | **`greenops_carbon_*`** |

Prometheus scrapes port 8002 via the `greenops-carbon-exporter` job in `monitoring/prometheus.yml`.

## Unavailability Handling

| Scenario | Behaviour |
|---|---|
| Carbon intensity API returns HTTP 5xx | `data_available=0`, error counter incremented, loop continues |
| Power breakdown API returns 4xx (restricted tier) | Intensity still exported normally; renewable/fossil gauges retain prior value |
| Network timeout | `data_available=0`, `error_type=timeout` counter incremented |
| Parse error (malformed JSON) | `data_available=0`, `error_type=parse` counter incremented |
| `update()` always | **Never raises** — scheduler and agent loop are never interrupted |

## Configuration

All configuration is via environment variables (see `/.env.example`):

| Variable | Default | Description |
|---|---|---|
| `ELECTRICITY_MAPS_API_KEY` | — | API key (required) |
| `ELECTRICITY_MAPS_ZONE` | `DE` | Grid zone |
| `ELECTRICITY_MAPS_BASE_URL` | `https://api.electricitymap.org/v3` | API base URL |
| `ELECTRICITY_MAPS_CACHE_TTL_SECONDS` | `300` | Cache TTL |

## Usage

### Standalone server

```bash
python -m carbon.server
```

### Programmatic

```python
from carbon.server import CarbonMetricsServer

server = CarbonMetricsServer(
    api_key=settings.electricity_maps.api_key,
    zone="DE",
    port=8002,
    poll_interval_seconds=60,
)
await server.run()  # blocks; handles SIGTERM gracefully
```

### Exporter only (embedded in agent)

```python
from carbon import CarbonMetricsExporter

exporter = CarbonMetricsExporter(api_key="...", zone="DE")
async with exporter:
    data = await exporter.update()
    print(f"Carbon intensity: {data.carbon_intensity_gco2_per_kwh} gCO2eq/kWh")
```

## Running Tests

```bash
pytest tests/unit/test_carbon_metrics.py -v
```

The test suite uses `respx` to mock all HTTP calls — no real Electricity Maps
API access is needed.
