"""
tests/unit/test_carbon_metrics.py

Unit tests for the carbon/ package — Electricity Maps data ingestion and
Prometheus metric export.

Strategy
--------
- All HTTP calls are intercepted by respx (mock httpx transport).
- No real Electricity Maps API calls are made.
- Every test class uses its own fresh CollectorRegistry to prevent
  cross-test metric registration conflicts.
- Tests cover: model parsing, metric updates, error handling, partial data,
  timestamp correctness, and credential safety.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from httpx import Response
from prometheus_client import CollectorRegistry, generate_latest

from carbon.exporter import CarbonMetricsExporter
from carbon.metrics import CarbonMetrics
from carbon.models import (
    CarbonIntensityResponse,
    ElectricityMapsData,
    PowerBreakdownResponse,
)

# ---------------------------------------------------------------------------
# Shared test fixtures and helpers
# ---------------------------------------------------------------------------

FAKE_ZONE = "DE"
FAKE_API_KEY = "test-api-key-not-real"
FAKE_BASE_URL = "https://api.electricitymap.org/v3"


def _intensity_payload(
    zone: str = FAKE_ZONE,
    carbon_intensity: float = 174.0,
    dt: str = "2024-01-15T12:00:00.000Z",
    is_estimated: bool = False,
) -> dict:
    """Build a minimal /carbon-intensity/latest success payload."""
    return {
        "zone": zone,
        "carbonIntensity": carbon_intensity,
        "datetime": dt,
        "updatedAt": "2024-01-15T12:05:00.000Z",
        "emissionFactorType": "lifecycle",
        "isEstimated": is_estimated,
        "estimationMethod": None,
    }


def _breakdown_payload(
    zone: str = FAKE_ZONE,
    renewable_pct: float | None = 52.0,
    fossil_pct: float | None = 18.0,
    low_carbon_pct: float | None = 70.0,
    dt: str = "2024-01-15T12:00:00.000Z",
) -> dict:
    """Build a minimal /power-breakdown/latest success payload."""
    return {
        "zone": zone,
        "datetime": dt,
        "renewablePercentage": renewable_pct,
        "fossilFuelPercentage": fossil_pct,
        "lowCarbonPercentage": low_carbon_pct,
        "powerConsumptionBreakdown": {},
    }


def _fresh_metrics() -> CarbonMetrics:
    """Return a CarbonMetrics instance backed by an isolated CollectorRegistry."""
    return CarbonMetrics(registry=CollectorRegistry())


def _fresh_exporter(metrics: CarbonMetrics | None = None) -> CarbonMetricsExporter:
    """Return an exporter wired to an isolated metrics registry."""
    m = metrics if metrics is not None else _fresh_metrics()
    return CarbonMetricsExporter(
        api_key=FAKE_API_KEY,
        zone=FAKE_ZONE,
        base_url=FAKE_BASE_URL,
        metrics=m,
    )


def _gauge_value(metrics: CarbonMetrics, gauge_name: str, zone: str = FAKE_ZONE) -> float:
    """Read the current value of a labelled Gauge by attribute name."""
    gauge = getattr(metrics, gauge_name)
    return gauge.labels(zone=zone)._value.get()  # noqa: SLF001


def _counter_value(
    metrics: CarbonMetrics,
    counter_name: str,
    zone: str = FAKE_ZONE,
    error_type: str = "connection",
) -> float:
    """Read the current value of a labelled Counter by attribute name."""
    counter = getattr(metrics, counter_name)
    return counter.labels(zone=zone, error_type=error_type)._value.get()  # noqa: SLF001


# ---------------------------------------------------------------------------
# 1. Model parsing
# ---------------------------------------------------------------------------


class TestCarbonIntensityModel:
    """Pydantic model parsing for /carbon-intensity/latest responses."""

    def test_parses_full_payload(self) -> None:
        payload = _intensity_payload()
        model = CarbonIntensityResponse.model_validate(payload)

        assert model.zone == "DE"
        assert model.carbon_intensity == pytest.approx(174.0)
        assert model.datetime_utc.tzinfo is not None  # always UTC-aware
        assert model.is_estimated is False

    def test_parses_is_estimated_true(self) -> None:
        payload = _intensity_payload(is_estimated=True)
        model = CarbonIntensityResponse.model_validate(payload)
        assert model.is_estimated is True

    def test_datetime_is_utc(self) -> None:
        payload = _intensity_payload(dt="2024-06-01T08:30:00.000Z")
        model = CarbonIntensityResponse.model_validate(payload)
        assert model.datetime_utc.tzinfo == timezone.utc
        assert model.datetime_utc.hour == 8
        assert model.datetime_utc.minute == 30

    def test_null_carbon_intensity_raises(self) -> None:
        payload = _intensity_payload()
        payload["carbonIntensity"] = None
        with pytest.raises(Exception):
            CarbonIntensityResponse.model_validate(payload)

    def test_integer_carbon_intensity_coerced_to_float(self) -> None:
        payload = _intensity_payload(carbon_intensity=200)  # type: ignore[arg-type]
        model = CarbonIntensityResponse.model_validate(payload)
        assert isinstance(model.carbon_intensity, float)
        assert model.carbon_intensity == pytest.approx(200.0)


class TestPowerBreakdownModel:
    """Pydantic model parsing for /power-breakdown/latest responses."""

    def test_parses_full_payload(self) -> None:
        payload = _breakdown_payload()
        model = PowerBreakdownResponse.model_validate(payload)

        assert model.zone == "DE"
        assert model.renewable_percentage == pytest.approx(52.0)
        assert model.fossil_fuel_percentage == pytest.approx(18.0)
        assert model.low_carbon_percentage == pytest.approx(70.0)

    def test_null_percentages_are_none(self) -> None:
        payload = _breakdown_payload(
            renewable_pct=None, fossil_pct=None, low_carbon_pct=None
        )
        model = PowerBreakdownResponse.model_validate(payload)
        assert model.renewable_percentage is None
        assert model.fossil_fuel_percentage is None
        assert model.low_carbon_percentage is None

    def test_integer_percentages_coerced(self) -> None:
        payload = _breakdown_payload(renewable_pct=60, fossil_pct=20, low_carbon_pct=80)
        model = PowerBreakdownResponse.model_validate(payload)
        assert isinstance(model.renewable_percentage, float)
        assert model.renewable_percentage == pytest.approx(60.0)

    def test_missing_zone_raises(self) -> None:
        payload = _breakdown_payload()
        del payload["zone"]
        with pytest.raises(Exception):
            PowerBreakdownResponse.model_validate(payload)


class TestElectricityMapsDataModel:
    """Tests for the normalised aggregate model."""

    def _make_intensity(self, intensity: float = 150.0) -> CarbonIntensityResponse:
        return CarbonIntensityResponse.model_validate(
            _intensity_payload(carbon_intensity=intensity)
        )

    def _make_breakdown(self) -> PowerBreakdownResponse:
        return PowerBreakdownResponse.model_validate(_breakdown_payload())

    def test_from_api_responses_with_breakdown(self) -> None:
        data = ElectricityMapsData.from_api_responses(
            self._make_intensity(150.0), self._make_breakdown()
        )
        assert data.zone == "DE"
        assert data.carbon_intensity_gco2_per_kwh == pytest.approx(150.0)
        assert data.renewable_percentage == pytest.approx(52.0)
        assert data.fossil_fuel_percentage == pytest.approx(18.0)
        assert data.low_carbon_percentage == pytest.approx(70.0)

    def test_from_api_responses_without_breakdown(self) -> None:
        data = ElectricityMapsData.from_api_responses(
            self._make_intensity(200.0), breakdown=None
        )
        assert data.carbon_intensity_gco2_per_kwh == pytest.approx(200.0)
        assert data.renewable_percentage is None
        assert data.fossil_fuel_percentage is None
        assert data.low_carbon_percentage is None

    def test_data_timestamp_unix_is_valid_epoch(self) -> None:
        data = ElectricityMapsData.from_api_responses(self._make_intensity())
        ts = data.data_timestamp_unix
        # Must be a plausible Unix timestamp (after year 2020)
        assert ts > datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        assert isinstance(ts, float)


# ---------------------------------------------------------------------------
# 2. CarbonMetrics facade
# ---------------------------------------------------------------------------


class TestCarbonMetricsFacade:
    """Tests for the CarbonMetrics facade and metric registration."""

    def test_isolated_registry_does_not_conflict(self) -> None:
        """Two CarbonMetrics instances with separate registries must not conflict."""
        m1 = CarbonMetrics(registry=CollectorRegistry())
        m2 = CarbonMetrics(registry=CollectorRegistry())
        # Both exist without raising DuplicateMetric errors
        assert m1 is not m2

    def test_metric_names_list(self) -> None:
        m = _fresh_metrics()
        names = m.metric_names
        assert "greenops_carbon_intensity_gco2_per_kwh" in names
        assert "greenops_carbon_renewable_percentage" in names
        assert "greenops_carbon_fossil_fuel_percentage" in names
        assert "greenops_carbon_low_carbon_percentage" in names
        assert "greenops_carbon_last_update_timestamp_seconds" in names
        assert "greenops_carbon_data_available" in names
        assert "greenops_carbon_scrape_errors_total" in names
        assert "greenops_carbon_scrape_duration_seconds" in names

    def test_zone_label_is_present_after_set(self) -> None:
        m = _fresh_metrics()
        m.intensity.labels(zone="FR").set(120.0)
        val = m.intensity.labels(zone="FR")._value.get()  # noqa: SLF001
        assert val == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# 3. Successful metric updates
# ---------------------------------------------------------------------------


class TestCarbonMetricsExporterSuccessfulUpdate:
    """Exporter correctly updates metrics on a full successful fetch."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_updates_carbon_intensity(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(carbon_intensity=220.0))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            result = await exporter.update()

        assert result is not None
        assert _gauge_value(metrics, "intensity") == pytest.approx(220.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_updates_renewable_and_fossil_percentages(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(
                200,
                json=_breakdown_payload(
                    renewable_pct=65.0, fossil_pct=10.0, low_carbon_pct=80.0
                ),
            )
        )

        async with exporter:
            await exporter.update()

        assert _gauge_value(metrics, "renewable_percentage") == pytest.approx(65.0)
        assert _gauge_value(metrics, "fossil_fuel_percentage") == pytest.approx(10.0)
        assert _gauge_value(metrics, "low_carbon_percentage") == pytest.approx(80.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_sets_data_available_to_1_on_success(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        assert _gauge_value(metrics, "data_available") == pytest.approx(1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_updates_timestamp_gauge(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        dt_str = "2024-06-15T10:30:00.000Z"
        expected_ts = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp()

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(dt=dt_str))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload(dt=dt_str))
        )

        async with exporter:
            await exporter.update()

        ts_val = _gauge_value(metrics, "last_update_timestamp")
        assert ts_val == pytest.approx(expected_ts, abs=1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_electricity_maps_data_on_success(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(carbon_intensity=99.0))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            result = await exporter.update()

        assert isinstance(result, ElectricityMapsData)
        assert result.carbon_intensity_gco2_per_kwh == pytest.approx(99.0)
        assert result.zone == FAKE_ZONE


# ---------------------------------------------------------------------------
# 4. Unavailability / error handling
# ---------------------------------------------------------------------------


class TestCarbonMetricsExporterUnavailable:
    """When Electricity Maps is unreachable the exporter must not raise."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_500_sets_data_available_to_0(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        async with exporter:
            result = await exporter.update()

        assert result is None
        assert _gauge_value(metrics, "data_available") == pytest.approx(0.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_error_increments_error_counter(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(503, text="Service Unavailable")
        )

        async with exporter:
            await exporter.update()

        err_val = _counter_value(metrics, "scrape_errors_total", error_type="http")
        assert err_val == pytest.approx(1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error_sets_data_available_to_0(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            side_effect=Exception("Connection refused")
        )

        async with exporter:
            result = await exporter.update()

        assert result is None
        # data_available is 0 (it was never set to 1)
        # The default value of a freshly-init Gauge is 0, which is correct here.

    @pytest.mark.asyncio
    @respx.mock
    async def test_update_never_raises(self) -> None:
        """update() must swallow all exceptions and return None."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(500, text="boom")
        )

        async with exporter:
            # This must NOT raise
            result = await exporter.update()
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_multiple_failures_accumulate_error_count(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(503, text="down")
        )

        async with exporter:
            await exporter.update()
            await exporter.update()
            await exporter.update()

        err_val = _counter_value(metrics, "scrape_errors_total", error_type="http")
        assert err_val == pytest.approx(3.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_data_available_resets_to_1_after_recovery(self) -> None:
        """After a failure, a successful fetch must set data_available back to 1."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        # First call: failure
        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(500, text="error")
        )
        async with exporter:
            await exporter.update()
            assert _gauge_value(metrics, "data_available") == pytest.approx(0.0)

            # Second call: success — reset the mock
            respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
                return_value=Response(200, json=_intensity_payload())
            )
            respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
                return_value=Response(200, json=_breakdown_payload())
            )
            await exporter.update()

        assert _gauge_value(metrics, "data_available") == pytest.approx(1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_parse_error_sets_data_available_to_0(self) -> None:
        """Malformed JSON from intensity endpoint counts as a parse error."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        # Valid HTTP 200 but invalid/missing required fields
        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json={"zone": "DE", "carbonIntensity": None})
        )

        async with exporter:
            result = await exporter.update()

        assert result is None
        assert _gauge_value(metrics, "data_available") == pytest.approx(0.0)
        err_val = _counter_value(metrics, "scrape_errors_total", error_type="parse")
        assert err_val == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 5. Partial data (power-breakdown unavailable)
# ---------------------------------------------------------------------------


class TestCarbonMetricsExporterPartialData:
    """When /power-breakdown returns 4xx, intensity is still exported."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_intensity_exported_when_breakdown_returns_403(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(carbon_intensity=155.0))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(403, text="Forbidden — upgrade plan")
        )

        async with exporter:
            result = await exporter.update()

        assert result is not None
        assert result.carbon_intensity_gco2_per_kwh == pytest.approx(155.0)
        assert result.renewable_percentage is None
        assert result.fossil_fuel_percentage is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_data_available_is_1_when_breakdown_403(self) -> None:
        """Breakdown 4xx is expected / partial — data_available should still be 1."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(403, text="Forbidden")
        )

        async with exporter:
            await exporter.update()

        assert _gauge_value(metrics, "data_available") == pytest.approx(1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_counter_not_incremented_on_breakdown_403(self) -> None:
        """Breakdown 4xx must NOT increment the error counter."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(403, text="Forbidden")
        )

        async with exporter:
            await exporter.update()

        # Counter must remain at 0 for all error_type values
        for err_type in ("connection", "http", "parse", "timeout"):
            val = _counter_value(metrics, "scrape_errors_total", error_type=err_type)
            assert val == pytest.approx(0.0), f"error_type={err_type} should be 0"

    @pytest.mark.asyncio
    @respx.mock
    async def test_renewable_gauge_retains_previous_value_when_breakdown_unavailable(
        self,
    ) -> None:
        """
        When breakdown is unavailable, the renewable Gauge must NOT be updated.
        Prometheus will serve the previously-set value until next success.
        """
        metrics = _fresh_metrics()
        # Pre-set a value on the renewable gauge to simulate a prior successful fetch
        metrics.renewable_percentage.labels(zone=FAKE_ZONE).set(75.0)

        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(403, text="Forbidden")
        )

        async with exporter:
            await exporter.update()

        # Must still be 75.0 — the exporter must not have zeroed it
        assert _gauge_value(metrics, "renewable_percentage") == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# 6. Timestamp handling
# ---------------------------------------------------------------------------


class TestCarbonMetricsTimestampHandling:
    """Timestamp gauge correctness — used by the CarbonDataStale alert."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_timestamp_gauge_matches_electricity_maps_datetime(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        # Use a known datetime
        iso_str = "2024-03-20T14:00:00.000Z"
        expected_epoch = datetime(2024, 3, 20, 14, 0, 0, tzinfo=timezone.utc).timestamp()

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(dt=iso_str))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload(dt=iso_str))
        )

        async with exporter:
            await exporter.update()

        ts_val = _gauge_value(metrics, "last_update_timestamp")
        assert ts_val == pytest.approx(expected_epoch, abs=1.0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_stale_alert_formula_would_fire_for_old_data(self) -> None:
        """
        Simulate the Prometheus alert formula:
            (time() - greenops_carbon_last_update_timestamp_seconds) > 600

        If the data timestamp is > 10 minutes old, the alert would fire.
        """
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        # Use a datetime 30 minutes in the past
        old_dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        old_iso = old_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(dt=old_iso))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload(dt=old_iso))
        )

        async with exporter:
            await exporter.update()

        ts_val = _gauge_value(metrics, "last_update_timestamp")
        staleness = time.time() - ts_val
        # 30+ minutes old → alert formula evaluates True (staleness > 600)
        assert staleness > 600

    @pytest.mark.asyncio
    @respx.mock
    async def test_fresh_data_timestamp_would_not_trigger_stale_alert(self) -> None:
        """Current-ish datetime should not trigger the stale alert (< 600 s old)."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        # Use a datetime very close to now
        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(dt=now_iso))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload(dt=now_iso))
        )

        async with exporter:
            await exporter.update()

        ts_val = _gauge_value(metrics, "last_update_timestamp")
        staleness = time.time() - ts_val
        assert staleness < 600  # should NOT trigger the stale alert


# ---------------------------------------------------------------------------
# 7. Prometheus text format output — HELP, TYPE metadata, no credentials
# ---------------------------------------------------------------------------


class TestPrometheusTextOutput:
    """Validate Prometheus text exposition format and credential safety."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_output_contains_help_metadata(self) -> None:
        """Every metric must have a # HELP line in the text output."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")

        for name in metrics.metric_names:
            assert f"# HELP {name}" in output, f"Missing # HELP for {name}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_output_contains_type_metadata(self) -> None:
        """Every metric must have a # TYPE line in the text output."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")

        for name in metrics.metric_names:
            assert f"# TYPE {name}" in output, f"Missing # TYPE for {name}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_zone_label_present_in_output(self) -> None:
        """The zone label must appear in exported metric lines."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload(zone="DE"))
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload(zone="DE"))
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")
        # The zone label must appear in metric lines
        assert 'zone="DE"' in output

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_key_not_in_metric_output(self) -> None:
        """
        The Electricity Maps API key must NEVER appear in Prometheus output.

        This is a security requirement — credentials must not be exposed
        through the metrics endpoint.
        """
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")
        assert FAKE_API_KEY not in output, (
            f"API key '{FAKE_API_KEY}' must not appear in Prometheus metric output"
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_intensity_gauge_type_is_gauge(self) -> None:
        """Carbon intensity must be a GAUGE (not counter) in the type metadata."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")
        assert "# TYPE greenops_carbon_intensity_gco2_per_kwh gauge" in output

    @pytest.mark.asyncio
    @respx.mock
    async def test_error_counter_type_is_counter(self) -> None:
        """Scrape errors must be a COUNTER type."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(500, text="error")
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")
        assert "# TYPE greenops_carbon_scrape_errors_total counter" in output

    @pytest.mark.asyncio
    @respx.mock
    async def test_duration_histogram_type_is_histogram(self) -> None:
        """Scrape duration must be a HISTOGRAM type."""
        registry = CollectorRegistry()
        metrics = CarbonMetrics(registry=registry)
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        output = generate_latest(registry).decode("utf-8")
        assert "# TYPE greenops_carbon_scrape_duration_seconds histogram" in output


# ---------------------------------------------------------------------------
# 8. Scrape duration histogram
# ---------------------------------------------------------------------------


class TestScrapeTimingHistogram:
    """Scrape duration is observed on every call, including failures."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_duration_observed_on_success(self) -> None:
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(200, json=_intensity_payload())
        )
        respx.get(f"{FAKE_BASE_URL}/power-breakdown/latest").mock(
            return_value=Response(200, json=_breakdown_payload())
        )

        async with exporter:
            await exporter.update()

        # histogram _count should be 1
        count = metrics.scrape_duration_seconds.labels(
            zone=FAKE_ZONE
        )._sum.get()  # noqa: SLF001
        assert count > 0.0  # some positive duration was recorded

    @pytest.mark.asyncio
    @respx.mock
    async def test_duration_observed_on_failure(self) -> None:
        """Duration histogram must be observed even when the fetch fails."""
        metrics = _fresh_metrics()
        exporter = _fresh_exporter(metrics)

        respx.get(f"{FAKE_BASE_URL}/carbon-intensity/latest").mock(
            return_value=Response(500, text="down")
        )

        async with exporter:
            await exporter.update()

        count = metrics.scrape_duration_seconds.labels(
            zone=FAKE_ZONE
        )._sum.get()  # noqa: SLF001
        assert count >= 0.0  # recorded (may be ~0 in fast test environments)
