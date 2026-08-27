"""
carbon — Electricity Maps carbon data ingestion and Prometheus export.

This package fetches real-time grid carbon-intensity data from the
Electricity Maps API and converts it into Prometheus-compatible metrics
that Prometheus can scrape and store as time-series.

Public API
----------
CarbonMetricsExporter   Fetches Electricity Maps data and updates metrics.
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

Usage::

    from carbon import CarbonMetricsExporter, CarbonMetrics

    exporter = CarbonMetricsExporter(
        api_key="...",      # from ELECTRICITY_MAPS_API_KEY
        zone="DE",
    )
    await exporter.update()  # call on each poll interval
"""

from carbon.exporter import CarbonMetricsExporter
from carbon.metrics import CarbonMetrics
from carbon.models import ElectricityMapsData

__all__ = [
    "CarbonMetricsExporter",
    "CarbonMetrics",
    "ElectricityMapsData",
]
