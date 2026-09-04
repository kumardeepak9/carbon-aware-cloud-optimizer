"""
carbon/models.py — Pydantic models for Electricity Maps API responses.

Electricity Maps exposes two relevant endpoints:

  GET /v3/carbon-intensity/latest?zone=<ZONE>
      Returns the current grid carbon intensity in gCO2eq/kWh.

  GET /v3/power-breakdown/latest?zone=<ZONE>
      Returns a breakdown of generation by source (renewable, fossil, etc.).

Both responses share a common envelope structure.  This module models only
the fields we export to Prometheus; unknown fields are ignored.

Reference: https://docs.electricitymap.org/api-reference
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

_ZONE_PATTERN = re.compile(r"^[A-Z0-9]{2,}(?:-[A-Z0-9]+)*$")


def validate_electricity_maps_zone(value: Any) -> str:
    """Validate a bounded Electricity Maps zone identifier."""
    if not isinstance(value, str):
        raise ValueError("zone must be a string")
    zone = value.strip().upper()
    if not zone:
        raise ValueError("zone must not be empty")
    if len(zone) > 64:
        raise ValueError("zone is too long")
    if not _ZONE_PATTERN.fullmatch(zone):
        raise ValueError("zone must contain only uppercase letters, numbers, and hyphens")
    return zone


def parse_utc_datetime(value: Any) -> datetime:
    """Parse ISO-8601 input and normalise it to an aware UTC datetime."""
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        dt = value
    else:
        raise ValueError(f"Cannot parse datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Carbon intensity (/carbon-intensity/latest)
# ---------------------------------------------------------------------------


class CarbonIntensityResponse(BaseModel):
    """
    Parsed response from GET /v3/carbon-intensity/latest.

    Example payload::

        {
            "zone": "DE",
            "carbonIntensity": 174,
            "datetime": "2024-01-15T12:00:00.000Z",
            "updatedAt": "2024-01-15T12:05:00.000Z",
            "emissionFactorType": "lifecycle",
            "isEstimated": false,
            "estimationMethod": null
        }
    """

    zone: str
    carbon_intensity: float = Field(alias="carbonIntensity")
    datetime_utc: datetime = Field(alias="datetime")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    is_estimated: bool = Field(default=False, alias="isEstimated")

    model_config = {"populate_by_name": True}

    @field_validator("zone", mode="before")
    @classmethod
    def validate_zone(cls, v: Any) -> str:
        return validate_electricity_maps_zone(v)

    @field_validator("carbon_intensity", mode="before")
    @classmethod
    def coerce_carbon_intensity(cls, v: Any) -> float:
        """Cast string or int values; raise on null so callers can handle it."""
        if v is None:
            raise ValueError("carbonIntensity is null")
        value = float(v)
        if value < 0:
            raise ValueError("carbonIntensity must be non-negative")
        return value

    @field_validator("datetime_utc", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Parse ISO-8601 string and normalise to UTC."""
        return parse_utc_datetime(v)


# ---------------------------------------------------------------------------
# Power breakdown (/power-breakdown/latest)
# ---------------------------------------------------------------------------


class PowerBreakdownResponse(BaseModel):
    """
    Parsed response from GET /v3/power-breakdown/latest.

    We capture only the summary percentage fields that are directly
    exportable as Prometheus Gauges.  The full per-source breakdown map
    is parsed but not individually exported to avoid unbounded label cardinality.

    Example payload snippet::

        {
            "zone": "DE",
            "datetime": "2024-01-15T12:00:00.000Z",
            "renewablePercentage": 52,
            "fossilFuelPercentage": 18,
            "lowCarbonPercentage": 70,
            "powerConsumptionBreakdown": { ... }
        }
    """

    zone: str
    datetime_utc: datetime = Field(alias="datetime")
    renewable_percentage: float | None = Field(default=None, alias="renewablePercentage")
    fossil_fuel_percentage: float | None = Field(default=None, alias="fossilFuelPercentage")
    low_carbon_percentage: float | None = Field(default=None, alias="lowCarbonPercentage")

    model_config = {"populate_by_name": True}

    @field_validator("zone", mode="before")
    @classmethod
    def validate_zone(cls, v: Any) -> str:
        return validate_electricity_maps_zone(v)

    @field_validator(
        "renewable_percentage", "fossil_fuel_percentage", "low_carbon_percentage", mode="before"
    )
    @classmethod
    def coerce_nullable_float(cls, v: Any) -> float | None:
        """Return None for null API values; cast numbers to float."""
        if v is None:
            return None
        try:
            value = float(v)
        except (TypeError, ValueError):
            return None
        if 0.0 <= value <= 100.0:
            return value
        return None

    @field_validator("datetime_utc", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Parse ISO-8601 string and normalise to UTC."""
        return parse_utc_datetime(v)


# ---------------------------------------------------------------------------
# Normalised aggregate — the object the exporter works with
# ---------------------------------------------------------------------------


class ElectricityMapsData(BaseModel):
    """
    Normalised snapshot of all Electricity Maps data for one zone.

    Aggregates data from both API endpoints.  Fields sourced from the
    power-breakdown endpoint are Optional — they will be None when that
    endpoint is unavailable (e.g. restricted API tier).

    This is the object that ``carbon/exporter.py`` receives and converts
    into Prometheus metric updates.
    """

    zone: str = Field(description="Electricity Maps zone identifier (e.g. 'DE', 'FR').")
    carbon_intensity_gco2_per_kwh: float = Field(description="Grid carbon intensity in gCO2eq/kWh.")
    renewable_percentage: float | None = Field(
        default=None,
        description="Percentage of generation from renewable sources (0–100).",
    )
    fossil_fuel_percentage: float | None = Field(
        default=None,
        description="Percentage of generation from fossil fuels (0–100).",
    )
    low_carbon_percentage: float | None = Field(
        default=None,
        description="Percentage of generation from low-carbon sources (renewable + nuclear).",
    )
    data_datetime_utc: datetime = Field(
        description="UTC datetime of the Electricity Maps data point."
    )
    is_estimated: bool = Field(
        default=False,
        description="True when Electricity Maps could not get real data and used an estimate.",
    )

    @field_validator("zone", mode="before")
    @classmethod
    def validate_zone(cls, v: Any) -> str:
        return validate_electricity_maps_zone(v)

    @field_validator("data_datetime_utc", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        return parse_utc_datetime(v)

    @property
    def data_timestamp_unix(self) -> float:
        """Unix epoch of the data datetime — used for the Prometheus timestamp gauge."""
        return self.data_datetime_utc.timestamp()

    @classmethod
    def from_api_responses(
        cls,
        intensity: CarbonIntensityResponse,
        breakdown: PowerBreakdownResponse | None = None,
    ) -> ElectricityMapsData:
        """
        Build an ElectricityMapsData from API response objects.

        Args:
            intensity:  Parsed carbon-intensity response (required).
            breakdown:  Parsed power-breakdown response (optional — None when
                        the endpoint is unavailable or the tier does not allow it).

        Returns:
            Normalised ElectricityMapsData ready for metric export.
        """
        matching_breakdown = breakdown if breakdown and breakdown.zone == intensity.zone else None
        return cls(
            zone=intensity.zone,
            carbon_intensity_gco2_per_kwh=intensity.carbon_intensity,
            renewable_percentage=(
                matching_breakdown.renewable_percentage if matching_breakdown else None
            ),
            fossil_fuel_percentage=(
                matching_breakdown.fossil_fuel_percentage if matching_breakdown else None
            ),
            low_carbon_percentage=(
                matching_breakdown.low_carbon_percentage if matching_breakdown else None
            ),
            data_datetime_utc=intensity.datetime_utc,
            is_estimated=intensity.is_estimated,
        )
