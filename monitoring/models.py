"""
monitoring/models.py — Pydantic models for the Prometheus HTTP API.

The Prometheus HTTP API always returns JSON with the envelope::

    {
      "status": "success" | "error",
      "data": {
        "resultType": "vector" | "matrix" | "scalar" | "string",
        "result": [...]
      },
      "errorType": "...",   # only on error
      "error":    "..."     # only on error
    }

These models parse and validate that envelope, making downstream code
type-safe without manual dict access.

Reference: https://prometheus.io/docs/prometheus/latest/querying/api/
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class PrometheusMetric(BaseModel):
    """A single metric label-set returned inside a result."""

    labels: dict[str, str] = Field(default_factory=dict, alias="metric")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Instant query (vector)
# ---------------------------------------------------------------------------


class InstantSample(BaseModel):
    """One sample from an instant (vector) query result."""

    metric: dict[str, str]
    value: tuple[float, str]  # (unix_timestamp, sample_value_string)


class VectorData(BaseModel):
    result_type: Literal["vector"] = Field(alias="resultType")
    result: list[InstantSample]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Range query (matrix)
# ---------------------------------------------------------------------------


class RangeSample(BaseModel):
    """One time-series from a range (matrix) query result."""

    metric: dict[str, str]
    values: list[tuple[float, str]]  # list of (timestamp, value_string) pairs


class MatrixData(BaseModel):
    result_type: Literal["matrix"] = Field(alias="resultType")
    result: list[RangeSample]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Scalar / String
# ---------------------------------------------------------------------------


class ScalarData(BaseModel):
    result_type: Literal["scalar"] = Field(alias="resultType")
    result: tuple[float, str]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Top-level API envelope
# ---------------------------------------------------------------------------


class PrometheusResponse(BaseModel):
    """
    Top-level Prometheus HTTP API response envelope.

    Supports both success and error payloads.
    """

    status: Literal["success", "error"]
    data: dict[str, Any] | None = None
    error_type: str | None = Field(default=None, alias="errorType")
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    def as_vector(self) -> VectorData:
        """Parse data as an instant query (vector) result."""
        if self.data is None:
            raise ValueError("Response has no data.")
        return VectorData.model_validate(self.data)

    def as_matrix(self) -> MatrixData:
        """Parse data as a range query (matrix) result."""
        if self.data is None:
            raise ValueError("Response has no data.")
        return MatrixData.model_validate(self.data)

    def as_scalar(self) -> ScalarData:
        """Parse data as a scalar result."""
        if self.data is None:
            raise ValueError("Response has no data.")
        return ScalarData.model_validate(self.data)


# ---------------------------------------------------------------------------
# Convenience result types consumed by the agent
# ---------------------------------------------------------------------------


class MetricSnapshot(BaseModel):
    """
    A single named metric value at a point in time.

    Used by the AI agent to make scaling decisions.
    """

    name: str = Field(description="Human-readable metric name (from GreenOpsQueries).")
    query: str = Field(description="The PromQL expression that produced this value.")
    value: float = Field(description="Parsed float value of the metric.")
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Label-set identifying the specific time-series.",
    )
    timestamp: float = Field(description="Unix timestamp of the sample.")
    unit: str = Field(default="", description="Optional unit string (e.g. 'cores', 'bytes').")


class AgentObservation(BaseModel):
    """
    Complete set of metric snapshots gathered in one agent poll cycle.

    The AI agent receives this as its 'state' input before making decisions.
    """

    snapshots: list[MetricSnapshot]
    collected_at: float = Field(description="Unix timestamp when collection completed.")
    namespace: str = Field(default="greenops")
    deployment: str = Field(default="greenops-demo-workload")
