"""
carbon/exporter.py — Electricity Maps → Prometheus metric exporter.

CarbonMetricsExporter
---------------------
Fetches real-time carbon data from the Electricity Maps HTTP API and
translates it into Prometheus metric updates.

Design principles
-----------------
1. **Safe unavailability handling**
   When the API is unreachable, returns an HTTP error, or returns unparseable
   data, the exporter:
   - Sets ``greenops_carbon_data_available{zone=...}`` to 0
   - Increments ``greenops_carbon_scrape_errors_total{zone=..., error_type=...}``
   - Leaves all other Gauges at their last-set value (Prometheus carries the
     last-observed value forward in time until a new scrape overwrites it)
   - Never raises an exception out of ``update()`` — the caller (APScheduler
     or the agent loop) does not crash on a temporary API outage.

2. **Partial data (restricted API tier)**
   If ``/carbon-intensity/latest`` succeeds but ``/power-breakdown/latest``
   returns 4xx (common on the free API tier), the exporter:
   - Exports carbon intensity, timestamp, and data_available normally
   - Leaves renewable/fossil/low-carbon Gauges at their previous value
   - Does NOT increment the error counter (partial success is not an error)

3. **Timestamp handling**
   The ``datetime`` field from Electricity Maps is the timestamp of the data
   point itself (not the fetch time).  It is parsed to UTC and stored as a
   Unix epoch in ``greenops_carbon_last_update_timestamp_seconds``.  This
   allows the existing ``CarbonDataStale`` alert rule to work unchanged:
       (time() - greenops_carbon_last_update_timestamp_seconds) > 600

4. **No credentials in metrics**
   The API key is stored in ``_api_key`` and passed only in HTTP headers.
   It is never written into any metric name, label, or value.

Usage::

    exporter = CarbonMetricsExporter(
        api_key="...",
        zone="DE",
        base_url="https://api.electricitymap.org/v3",
    )
    await exporter.update()   # call on each poll interval (60 s recommended)

    # Or as async context manager for one-shot use:
    async with CarbonMetricsExporter(api_key="...", zone="DE") as exporter:
        await exporter.update()
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from carbon.metrics import CarbonMetrics
from carbon.models import (
    CarbonIntensityResponse,
    ElectricityMapsData,
    PowerBreakdownResponse,
    validate_electricity_maps_zone,
)
from config import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Error-type constants (used as the error_type label on the error counter)
# ---------------------------------------------------------------------------
_ERR_CONNECTION = "connection"
_ERR_HTTP = "http"
_ERR_PARSE = "parse"
_ERR_RATE_LIMIT = "rate_limit"
_ERR_STALE = "stale"
_ERR_TIMEOUT = "timeout"


class CarbonMetricsExporter:
    """
    Fetches Electricity Maps carbon data and updates Prometheus metrics.

    Can be used as an async context manager::

        async with CarbonMetricsExporter(api_key="...", zone="DE") as exp:
            await exp.update()

    Or with explicit lifecycle management::

        exp = CarbonMetricsExporter(api_key="...", zone="DE")
        await exp.open()
        await exp.update()
        await exp.close()

    Args:
        api_key:         Electricity Maps API key (from ELECTRICITY_MAPS_API_KEY).
                         Never exposed through metrics.
        zone:            Grid zone identifier (e.g. "DE", "FR", "US-CAL-CISO").
        base_url:        Electricity Maps API base URL.
        timeout_seconds: Per-request HTTP timeout.
        metrics:         CarbonMetrics facade instance.  Defaults to a new instance
                         using the global registry.  Inject a custom instance for
                         testing with an isolated registry.
    """

    _INTENSITY_PATH = "/carbon-intensity/latest"
    _BREAKDOWN_PATH = "/power-breakdown/latest"

    def __init__(
        self,
        api_key: str,
        zone: str = "DE",
        base_url: str = "https://api.electricitymap.org/v3",
        timeout_seconds: float = 10.0,
        max_data_age_seconds: float | None = None,
        metrics: CarbonMetrics | None = None,
    ) -> None:
        # Store API key privately — never logged, never included in metrics
        self._api_key = api_key
        self._zone = validate_electricity_maps_zone(zone)
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_data_age_seconds = max_data_age_seconds
        self._metrics = metrics if metrics is not None else CarbonMetrics()
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Async context manager / lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the underlying HTTP connection pool."""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={
                "auth-token": self._api_key,
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> CarbonMetricsExporter:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                "CarbonMetricsExporter not opened. "
                "Use 'async with CarbonMetricsExporter(...)' or call 'await exporter.open()'."
            )
        return self._http

    # ------------------------------------------------------------------
    # Public: main update method
    # ------------------------------------------------------------------

    async def update(self) -> ElectricityMapsData | None:
        """
        Fetch Electricity Maps data and update all Prometheus metrics.

        This method is designed to be called on a recurring schedule.  It
        never raises an exception — errors are recorded in the error counter
        and ``data_available`` is set to 0.

        Returns:
            The normalised ``ElectricityMapsData`` on success, or ``None`` if
            the carbon-intensity fetch failed.  Callers may inspect the return
            value but are not required to; all metric updates happen as a
            side effect.
        """
        t0 = time.perf_counter()
        try:
            data = await self._fetch_and_parse()
        except Exception as exc:  # noqa: BLE001 — all exceptions handled below
            # Any unhandled error from _fetch_and_parse is unexpected; log and
            # mark data as unavailable without crashing the scheduler.
            log.error(
                "carbon.exporter.unexpected_error",
                zone=self._zone,
                error=self._safe_error(exc),
                exc_info=True,
            )
            self._mark_unavailable(_ERR_PARSE)
            return None
        finally:
            duration = time.perf_counter() - t0
            self._metrics.scrape_duration_seconds.labels(zone=self._zone).observe(duration)

        if data is None:
            # _fetch_and_parse already updated metrics and logged the error
            return None

        if self._is_stale(data):
            self._mark_unavailable(_ERR_STALE)
            age = time.time() - data.data_timestamp_unix
            log.warning(
                "carbon.exporter.stale_data",
                zone=self._zone,
                data_age_seconds=round(age, 3),
                max_data_age_seconds=self._max_data_age_seconds,
            )
            return None

        self._update_metrics(data)
        log.info(
            "carbon.exporter.update_success",
            zone=self._zone,
            carbon_intensity=data.carbon_intensity_gco2_per_kwh,
            renewable_pct=data.renewable_percentage,
            fossil_pct=data.fossil_fuel_percentage,
            is_estimated=data.is_estimated,
        )
        return data

    # ------------------------------------------------------------------
    # Private: fetch + parse
    # ------------------------------------------------------------------

    async def _fetch_and_parse(self) -> ElectricityMapsData | None:
        """
        Fetch from Electricity Maps and return normalised data.

        Returns:
            ElectricityMapsData on success, None on fetch failure.
            All error metrics are updated before returning None.
        """
        params = {"zone": self._zone}

        # ── 1. Carbon intensity (required) ───────────────────────────
        intensity = await self._fetch_carbon_intensity(params)
        if intensity is None:
            return None  # error already recorded inside _fetch_carbon_intensity

        # ── 2. Power breakdown (optional) ────────────────────────────
        breakdown = await self._fetch_power_breakdown(params)
        # breakdown is None on failure — this is NOT counted as an error

        return ElectricityMapsData.from_api_responses(intensity, breakdown)

    async def _fetch_carbon_intensity(
        self, params: dict
    ) -> CarbonIntensityResponse | None:
        """
        Fetch /carbon-intensity/latest.

        Sets data_available=0 and increments error counter on failure.
        Returns None on any failure; the caller propagates None up to update().
        """
        try:
            resp = await self._client.get(self._INTENSITY_PATH, params=params)
            resp.raise_for_status()
        except httpx.TimeoutException:
            log.warning(
                "carbon.exporter.intensity_timeout",
                zone=self._zone,
                path=self._INTENSITY_PATH,
            )
            self._mark_unavailable(_ERR_TIMEOUT)
            return None
        except httpx.HTTPStatusError as exc:
            error_type = _ERR_RATE_LIMIT if exc.response.status_code == 429 else _ERR_HTTP
            log.warning(
                "carbon.exporter.intensity_http_error",
                zone=self._zone,
                status_code=exc.response.status_code,
            )
            self._mark_unavailable(error_type)
            return None
        except httpx.HTTPError as exc:
            log.warning(
                "carbon.exporter.intensity_connection_error",
                zone=self._zone,
                error=self._safe_error(exc),
            )
            self._mark_unavailable(_ERR_CONNECTION)
            return None

        try:
            return CarbonIntensityResponse.model_validate(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "carbon.exporter.intensity_parse_error",
                zone=self._zone,
                error=self._safe_error(exc),
            )
            self._mark_unavailable(_ERR_PARSE)
            return None

    async def _fetch_power_breakdown(
        self, params: dict
    ) -> PowerBreakdownResponse | None:
        """
        Fetch /power-breakdown/latest.

        On failure (e.g. 4xx from restricted API tier, parse error, timeout),
        returns None WITHOUT updating the error counter.  Partial data is an
        expected and valid state — not an error condition.
        """
        try:
            resp = await self._client.get(self._BREAKDOWN_PATH, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx on this endpoint is expected on free/restricted API tiers
            log.debug(
                "carbon.exporter.breakdown_unavailable",
                zone=self._zone,
                status_code=exc.response.status_code,
            )
            return None
        except httpx.HTTPError as exc:
            log.debug(
                "carbon.exporter.breakdown_http_error",
                zone=self._zone,
                error=self._safe_error(exc),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "carbon.exporter.breakdown_unexpected",
                zone=self._zone,
                error=self._safe_error(exc),
            )
            return None

        try:
            return PowerBreakdownResponse.model_validate(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "carbon.exporter.breakdown_parse_error",
                zone=self._zone,
                error=self._safe_error(exc),
            )
            return None

    # ------------------------------------------------------------------
    # Private: metric update
    # ------------------------------------------------------------------

    def _update_metrics(self, data: ElectricityMapsData) -> None:
        """
        Apply all metric updates from a successfully fetched ElectricityMapsData.

        Only updates renewable/fossil/low-carbon Gauges when the values are
        present (i.e. the power-breakdown endpoint was available).  When those
        fields are None, the Gauges retain their last-set value — Prometheus
        carries the last-seen value forward in time automatically.
        """
        zone = data.zone

        # Always updated on success
        self._metrics.intensity.labels(zone=zone).set(
            data.carbon_intensity_gco2_per_kwh
        )
        self._metrics.last_update_timestamp.labels(zone=zone).set(
            data.data_timestamp_unix
        )
        self._metrics.data_available.labels(zone=zone).set(1)

        # Optional — only when power-breakdown data is available
        if data.renewable_percentage is not None:
            self._metrics.renewable_percentage.labels(zone=zone).set(
                data.renewable_percentage
            )
        if data.fossil_fuel_percentage is not None:
            self._metrics.fossil_fuel_percentage.labels(zone=zone).set(
                data.fossil_fuel_percentage
            )
        if data.low_carbon_percentage is not None:
            self._metrics.low_carbon_percentage.labels(zone=zone).set(
                data.low_carbon_percentage
            )

    def _mark_unavailable(self, error_type: str) -> None:
        """Record a failed or unsafe carbon fetch without raising."""
        self._metrics.data_available.labels(zone=self._zone).set(0)
        self._metrics.scrape_errors_total.labels(
            zone=self._zone, error_type=error_type
        ).inc()

    def _is_stale(self, data: ElectricityMapsData) -> bool:
        if self._max_data_age_seconds is None:
            return False
        return time.time() - data.data_timestamp_unix > self._max_data_age_seconds

    def _safe_error(self, exc: BaseException) -> str:
        message = str(exc)
        if self._api_key:
            message = message.replace(self._api_key, "[redacted]")
        return message
