"""Typed inputs and read-only outputs for GreenOps decisions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator(
        "carbon_intensity_gco2_kwh",
        "renewable_percentage",
        "fossil_fuel_percentage",
        "low_carbon_percentage",
        "data_timestamp_seconds",
    )
    @classmethod
    def non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("environmental values must be non-negative")
        return value

    @field_validator("renewable_percentage", "fossil_fuel_percentage", "low_carbon_percentage")
    @classmethod
    def percentage_bounds(cls, value: float | None) -> float | None:
        if value is not None and value > 100:
            raise ValueError("percentage values must be between 0 and 100")
        return value


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

    @field_validator(
        "current_replicas",
        "ready_replicas",
        "availability_ratio",
        "cpu_request_ratio",
        "memory_request_ratio",
        "request_rate_rps",
        "error_rate_rps",
        "p99_latency_seconds",
        "restart_rate",
        "node_cpu_utilization_ratio",
        "node_memory_available_bytes",
    )
    @classmethod
    def non_negative(cls, value: float | int | None) -> float | int | None:
        if value is not None and value < 0:
            raise ValueError("operational values must be non-negative")
        return value

    @field_validator("availability_ratio", "node_cpu_utilization_ratio")
    @classmethod
    def ratio_bounds(cls, value: float | None) -> float | None:
        if value is not None and value > 1:
            raise ValueError("ratio values must be between 0 and 1")
        return value


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

    @field_validator("reason")
    @classmethod
    def reason_must_be_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("decision reason must not be empty")
        return value

    @model_validator(mode="after")
    def scale_actions_require_targets(self) -> DecisionRecommendation:
        if self.current_replicas is None and self.action is not Action.DEFER:
            raise ValueError("non-deferred decisions must include current replicas")
        if self.action in {Action.SCALE_DOWN, Action.SCALE_UP}:
            if self.recommended_replicas is None:
                raise ValueError("scale decisions must include recommended replicas")
            if self.recommended_replicas == self.current_replicas:
                raise ValueError("scale decisions must change replica count")
            # The action label and the replica delta must agree. The safety
            # policy applies its scale-down guards (CPU, latency, health, error
            # rate, restarts, max scale-down %) by inspecting the action; a
            # recommendation that shrinks the workload while labelled SCALE_UP
            # would skip every one of them. Reject the contradiction here so no
            # such recommendation can be constructed or deserialised.
            if self.current_replicas is not None:
                if (
                    self.action is Action.SCALE_UP
                    and self.recommended_replicas < self.current_replicas
                ):
                    raise ValueError(
                        "SCALE_UP must not decrease replicas "
                        f"({self.current_replicas} -> {self.recommended_replicas})"
                    )
                if (
                    self.action is Action.SCALE_DOWN
                    and self.recommended_replicas > self.current_replicas
                ):
                    raise ValueError(
                        "SCALE_DOWN must not increase replicas "
                        f"({self.current_replicas} -> {self.recommended_replicas})"
                    )
        return self


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
