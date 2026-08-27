"""
carbon/metrics.py — Prometheus metric definitions for the GreenOps carbon exporter.

All metrics are registered once at module import time and share the
``greenops_carbon_`` prefix so they are trivially filterable in Grafana.

Naming follows Prometheus best-practice convention:
    <namespace>_<subsystem>_<name>_<unit>

Label design
------------
Only ``zone`` is used as a label.  Zone values come from the operator-supplied
``ELECTRICITY_MAPS_ZONE`` setting — a bounded, known set (e.g. "DE", "FR").
No user-supplied, request-derived, or unbounded label values are ever attached,
so label cardinality stays constant and predictable.

Security note
-------------
The Electricity Maps API key is read from the environment and passed only to
the HTTP client.  It is NEVER written into any metric name, label, or value.

Metric inventory
----------------
greenops_carbon_intensity_gco2_per_kwh      Gauge
    Current grid carbon intensity in gCO2eq/kWh.  This is the primary signal
    consumed by the GreenOps AI agent and its alert rules.

greenops_carbon_renewable_percentage        Gauge
    Percentage of electricity generation from renewable sources (0–100).
    Set only when the Electricity Maps power-breakdown endpoint is available.

greenops_carbon_fossil_fuel_percentage      Gauge
    Percentage of electricity generation from fossil fuels (0–100).
    Set only when the power-breakdown endpoint is available.

greenops_carbon_low_carbon_percentage       Gauge
    Percentage from low-carbon sources (renewables + nuclear, 0–100).
    Set only when the power-breakdown endpoint is available.

greenops_carbon_last_update_timestamp_seconds  Gauge
    Unix epoch of the most recent Electricity Maps data point.
    Used by the ``CarbonDataStale`` alert: (time() - this_gauge) > 600.

greenops_carbon_data_available              Gauge
    1 if the last fetch returned valid, parseable data; 0 otherwise.
    Flips to 0 on HTTP errors, parse failures, or network timeouts.

greenops_carbon_scrape_errors_total         Counter
    Cumulative count of fetch or parse failures, labelled by error_type.

greenops_carbon_scrape_duration_seconds     Histogram
    Duration of each Electricity Maps API fetch cycle.
"""

from __future__ import annotations

from typing import Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Module-level default metric objects (use the global default registry)
# ---------------------------------------------------------------------------

CARBON_INTENSITY = Gauge(
    name="greenops_carbon_intensity_gco2_per_kwh",
    documentation=(
        "Real-time grid carbon intensity in gCO2eq/kWh, sourced from Electricity Maps. "
        "Primary input to GreenOps AI agent scaling decisions."
    ),
    labelnames=["zone"],
)

CARBON_RENEWABLE_PERCENTAGE = Gauge(
    name="greenops_carbon_renewable_percentage",
    documentation=(
        "Percentage of electricity generation from renewable sources (0–100). "
        "Available when the Electricity Maps power-breakdown endpoint is accessible."
    ),
    labelnames=["zone"],
)

CARBON_FOSSIL_FUEL_PERCENTAGE = Gauge(
    name="greenops_carbon_fossil_fuel_percentage",
    documentation=(
        "Percentage of electricity generation from fossil fuels (0–100). "
        "Available when the Electricity Maps power-breakdown endpoint is accessible."
    ),
    labelnames=["zone"],
)

CARBON_LOW_CARBON_PERCENTAGE = Gauge(
    name="greenops_carbon_low_carbon_percentage",
    documentation=(
        "Percentage of electricity generation from low-carbon sources "
        "(renewables + nuclear, 0–100). "
        "Available when the Electricity Maps power-breakdown endpoint is accessible."
    ),
    labelnames=["zone"],
)

CARBON_LAST_UPDATE_TIMESTAMP = Gauge(
    name="greenops_carbon_last_update_timestamp_seconds",
    documentation=(
        "Unix timestamp (seconds since epoch) of the most recent Electricity Maps "
        "data point. Used by the CarbonDataStale alert: "
        "(time() - greenops_carbon_last_update_timestamp_seconds) > 600."
    ),
    labelnames=["zone"],
)

CARBON_DATA_AVAILABLE = Gauge(
    name="greenops_carbon_data_available",
    documentation=(
        "1 if the last Electricity Maps fetch returned valid, parseable data; "
        "0 if the fetch failed (network error, parse error, HTTP error). "
        "Resets to 1 on the next successful fetch."
    ),
    labelnames=["zone"],
)

CARBON_SCRAPE_ERRORS_TOTAL = Counter(
    name="greenops_carbon_scrape_errors_total",
    documentation=(
        "Cumulative count of Electricity Maps fetch or parse failures. "
        "Labelled by error_type: 'connection', 'http', 'parse', 'timeout'."
    ),
    labelnames=["zone", "error_type"],
)

CARBON_SCRAPE_DURATION_SECONDS = Histogram(
    name="greenops_carbon_scrape_duration_seconds",
    documentation="Duration of each Electricity Maps API fetch cycle in seconds.",
    labelnames=["zone"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)


# ---------------------------------------------------------------------------
# Facade class — mirrors AgentMetrics pattern for clean injection / testing
# ---------------------------------------------------------------------------


class CarbonMetrics:
    """
    Facade that groups all greenops_carbon_* metric objects.

    Instantiate with an optional ``registry`` for test isolation::

        from prometheus_client import CollectorRegistry
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)

    In production (and at module level), use the default registry::

        from carbon.metrics import CarbonMetrics
        metrics = CarbonMetrics()

    Attributes mirror the module-level metric names for easy access and
    allow the exporter to be tested with isolated registries.
    """

    def __init__(self, registry: Optional[CollectorRegistry] = None) -> None:
        """
        Initialise metric objects.

        Args:
            registry: If provided, all metrics are registered in this isolated
                      registry (useful for unit tests).  If None, the global
                      prometheus_client default registry is used.
        """
        kwargs: dict = {"registry": registry} if registry is not None else {}

        self.intensity = Gauge(
            "greenops_carbon_intensity_gco2_per_kwh",
            (
                "Real-time grid carbon intensity in gCO2eq/kWh, sourced from "
                "Electricity Maps. Primary input to GreenOps AI agent scaling decisions."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.renewable_percentage = Gauge(
            "greenops_carbon_renewable_percentage",
            (
                "Percentage of electricity generation from renewable sources (0–100). "
                "Available when the Electricity Maps power-breakdown endpoint is accessible."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.fossil_fuel_percentage = Gauge(
            "greenops_carbon_fossil_fuel_percentage",
            (
                "Percentage of electricity generation from fossil fuels (0–100). "
                "Available when the Electricity Maps power-breakdown endpoint is accessible."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.low_carbon_percentage = Gauge(
            "greenops_carbon_low_carbon_percentage",
            (
                "Percentage of electricity generation from low-carbon sources "
                "(renewables + nuclear, 0–100). "
                "Available when the Electricity Maps power-breakdown endpoint is accessible."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.last_update_timestamp = Gauge(
            "greenops_carbon_last_update_timestamp_seconds",
            (
                "Unix timestamp (seconds since epoch) of the most recent Electricity Maps "
                "data point. Used by the CarbonDataStale alert: "
                "(time() - greenops_carbon_last_update_timestamp_seconds) > 600."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.data_available = Gauge(
            "greenops_carbon_data_available",
            (
                "1 if the last Electricity Maps fetch returned valid, parseable data; "
                "0 if the fetch failed (network error, parse error, HTTP error). "
                "Resets to 1 on the next successful fetch."
            ),
            labelnames=["zone"],
            **kwargs,
        )
        self.scrape_errors_total = Counter(
            "greenops_carbon_scrape_errors_total",
            (
                "Cumulative count of Electricity Maps fetch or parse failures. "
                "Labelled by error_type: 'connection', 'http', 'parse', 'timeout'."
            ),
            labelnames=["zone", "error_type"],
            **kwargs,
        )
        self.scrape_duration_seconds = Histogram(
            "greenops_carbon_scrape_duration_seconds",
            "Duration of each Electricity Maps API fetch cycle in seconds.",
            labelnames=["zone"],
            buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
            **kwargs,
        )

    @property
    def metric_names(self) -> list[str]:
        """Return all metric base names produced by this facade (for docs/testing)."""
        return [
            "greenops_carbon_intensity_gco2_per_kwh",
            "greenops_carbon_renewable_percentage",
            "greenops_carbon_fossil_fuel_percentage",
            "greenops_carbon_low_carbon_percentage",
            "greenops_carbon_last_update_timestamp_seconds",
            "greenops_carbon_data_available",
            "greenops_carbon_scrape_errors_total",
            "greenops_carbon_scrape_duration_seconds",
        ]
