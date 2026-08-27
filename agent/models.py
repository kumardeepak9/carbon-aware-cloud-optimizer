"""Typed inputs and read-only outputs for GreenOps decisions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Action(StrEnum):
    KEEP = "KEEP"
    SCALE_DOWN = "SCALE_DOWN"
    SCALE_UP = "SCALE_UP"
    DEFER = "DEFER"


class ValidationStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"


class EnvironmentalContext(BaseModel):
    """Grid information used to assess the carbon opportunity."""

    carbon_intensity_gco2_kwh: float | None = None
    region: str | None = None
    renewable_percentage: float | None = None
    fossil_fuel_percentage: float | None = None
    low_carbon_percentage: float | None = None
    data_available: bool | None = None
    data_timestamp_seconds: float | None = None


class OperationalContext(BaseModel):
    """Prometheus-derived workload and cluster state."""

    current_replicas: int | None = None
    ready_replicas: int | None = None
    availability_ratio: float | None = None
    cpu_request_ratio: float | None = None
    memory_request_ratio: float | None = None
    request_rate_rps: float | None = None
    error_rate_rps: float | None = None
    p99_latency_seconds: float | None = None
    restart_rate: float | None = None
    node_cpu_utilization_ratio: float | None = None
    node_memory_available_bytes: float | None = None


class DecisionMetadata(BaseModel):
    """Auditable policy metadata; this agent never executes recommendations."""

    policy_version: str = "phase-6-v1"
    read_only: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    missing_signals: list[str] = Field(default_factory=list)
    safety_guards_triggered: list[str] = Field(default_factory=list)
    decision_basis: str


class DecisionRecommendation(BaseModel):
    """The complete structured response returned by the GreenOps agent."""

    action: Action
    current_replicas: int | None = None
    recommended_replicas: int | None = None
    reason: str
    environmental_context: EnvironmentalContext
    operational_context: OperationalContext
    metadata: DecisionMetadata


class PolicyValidation(BaseModel):
    """Deterministic safety verdict required before any GitOps action exists."""

    status: ValidationStatus
    reason: str
    policy_version: str = "phase-7-v1"
    approved_for_gitops_change: bool = False
    safeguards_triggered: list[str] = Field(default_factory=list)
    evaluated_at_seconds: float


class ValidatedRecommendation(BaseModel):
    """AI recommendation plus mandatory Phase 7 policy validation."""

    recommendation: DecisionRecommendation
    validation: PolicyValidation
