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

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")
    is_estimated: bool = Field(default=False, alias="isEstimated")

    model_config = {"populate_by_name": True}

    @field_validator("carbon_intensity", mode="before")
    @classmethod
    def coerce_carbon_intensity(cls, v: Any) -> float:
        """Cast string or int values; raise on null so callers can handle it."""
        if v is None:
            raise ValueError("carbonIntensity is null")
        return float(v)

    @field_validator("datetime_utc", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Parse ISO-8601 string and normalise to UTC."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        elif isinstance(v, datetime):
            dt = v
        else:
            raise ValueError(f"Cannot parse datetime: {v!r}")
        return dt.astimezone(timezone.utc)


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
    renewable_percentage: Optional[float] = Field(
        default=None, alias="renewablePercentage"
    )
    fossil_fuel_percentage: Optional[float] = Field(
        default=None, alias="fossilFuelPercentage"
    )
    low_carbon_percentage: Optional[float] = Field(
        default=None, alias="lowCarbonPercentage"
    )

    model_config = {"populate_by_name": True}

    @field_validator("renewable_percentage", "fossil_fuel_percentage", "low_carbon_percentage", mode="before")
    @classmethod
    def coerce_nullable_float(cls, v: Any) -> Optional[float]:
        """Return None for null API values; cast numbers to float."""
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @field_validator("datetime_utc", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        """Parse ISO-8601 string and normalise to UTC."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        elif isinstance(v, datetime):
            dt = v
        else:
            raise ValueError(f"Cannot parse datetime: {v!r}")
        return dt.astimezone(timezone.utc)


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
    carbon_intensity_gco2_per_kwh: float = Field(
        description="Grid carbon intensity in gCO2eq/kWh."
    )
    renewable_percentage: Optional[float] = Field(
        default=None,
        description="Percentage of generation from renewable sources (0–100).",
    )
    fossil_fuel_percentage: Optional[float] = Field(
        default=None,
        description="Percentage of generation from fossil fuels (0–100).",
    )
    low_carbon_percentage: Optional[float] = Field(
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

    @property
    def data_timestamp_unix(self) -> float:
        """Unix epoch of the data datetime — used for the Prometheus timestamp gauge."""
        return self.data_datetime_utc.timestamp()

    @classmethod
    def from_api_responses(
        cls,
        intensity: CarbonIntensityResponse,
        breakdown: Optional[PowerBreakdownResponse] = None,
    ) -> "ElectricityMapsData":
        """
        Build an ElectricityMapsData from API response objects.

        Args:
            intensity:  Parsed carbon-intensity response (required).
            breakdown:  Parsed power-breakdown response (optional — None when
                        the endpoint is unavailable or the tier does not allow it).

        Returns:
            Normalised ElectricityMapsData ready for metric export.
        """
        return cls(
            zone=intensity.zone,
            carbon_intensity_gco2_per_kwh=intensity.carbon_intensity,
            renewable_percentage=breakdown.renewable_percentage if breakdown else None,
            fossil_fuel_percentage=breakdown.fossil_fuel_percentage if breakdown else None,
            low_carbon_percentage=breakdown.low_carbon_percentage if breakdown else None,
            data_datetime_utc=intensity.datetime_utc,
            is_estimated=intensity.is_estimated,
        )
