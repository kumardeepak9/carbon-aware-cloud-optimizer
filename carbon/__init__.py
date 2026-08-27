"""
carbon — Electricity Maps carbon data ingestion and Prometheus export.

This package fetches real-time grid carbon-intensity data from the
Electricity Maps API and converts it into Prometheus-compatible metrics
that Prometheus can scrape and store as time-series.

Public API
----------
CarbonMetricsExporter   Fetches Electricity Maps data and updates metrics.
CarbonMetricsServer     HTTP exposition server — serves metrics on port 8002.
CarbonMetrics           Facade grouping all greenops_carbon_* metric objects.
ElectricityMapsData     Normalised, validated data model for one API snapshot.

Metric series produced
----------------------
  greenops_carbon_intensity_gco2_per_kwh
  greenops_carbon_renewable_percentage
  greenops_carbon_fossil_fuel_percentage
  greenops_carbon_low_carbon_percentage
  greenops_carbon_last_update_timestamp_seconds
  greenops_carbon_data_available
  greenops_carbon_scrape_errors_total
  greenops_carbon_scrape_duration_seconds

Port allocation
---------------
  8000  Demo workload metrics  (app/)
  8001  AI agent metrics       (agent/)
  8002  Carbon metrics         (carbon/server.py)  ← this package

Usage::

    from carbon import CarbonMetricsServer

    server = CarbonMetricsServer(
        api_key="...",      # from ELECTRICITY_MAPS_API_KEY
        zone="DE",
        port=8002,
    )
    await server.run()      # blocks; handles SIGTERM gracefully
"""

from carbon.exporter import CarbonMetricsExporter
from carbon.metrics import CarbonMetrics
from carbon.models import ElectricityMapsData
from carbon.server import CarbonMetricsServer

__all__ = [
    "CarbonMetricsExporter",
    "CarbonMetricsServer",
    "CarbonMetrics",
    "ElectricityMapsData",
]
