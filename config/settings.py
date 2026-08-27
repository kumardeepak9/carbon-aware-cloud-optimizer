"""
config/settings.py — Application-wide configuration via environment variables.

All configuration is sourced from environment variables (or a .env file in
development).  No secrets or defaults that expose internal infrastructure are
hardcoded here.

Usage::

    from config import get_settings

    settings = get_settings()
    print(settings.app_env)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Top-level application settings."""

    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
    )
    log_format: Literal["json", "pretty"] = Field(
        default="json",
        description="'json' for structured production logs; 'pretty' for local dev.",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class ElectricityMapsSettings(BaseSettings):
    """Settings for the Electricity Maps API client."""

    api_key: str = Field(
        description="Electricity Maps API key — set via ELECTRICITY_MAPS_API_KEY.",
    )
    base_url: str = Field(
        default="https://api.electricitymap.org/v3",
    )
    zone: str = Field(
        default="DE",
        description="Default grid zone (ISO 3166-1 alpha-2 or Electricity Maps zone ID).",
    )
    cache_ttl_seconds: int = Field(
        default=300,
        ge=30,
        description="TTL for cached carbon-intensity responses.",
    )

    model_config = SettingsConfigDict(
        env_prefix="ELECTRICITY_MAPS_",
        env_file=".env",
        extra="ignore",
    )


class KubernetesSettings(BaseSettings):
    """Settings for Kubernetes API access."""

    in_cluster: bool = Field(
        default=False,
        description="Set to true when running inside a Kubernetes pod.",
    )
    kubeconfig_path: str = Field(
        default="~/.kube/config",
        description="Ignored when in_cluster=true.",
    )
    namespace: str = Field(
        default="greenops",
        description="Default namespace for workload operations.",
    )

    model_config = SettingsConfigDict(
        env_prefix="K8S_",
        env_file=".env",
        extra="ignore",
    )


class PrometheusSettings(BaseSettings):
    """Settings for Prometheus metrics exposition."""

    api_url: str = Field(
        default="http://localhost:9090",
        description="Prometheus HTTP API used by the read-only decision agent.",
    )
    pushgateway_url: str = Field(
        default="http://localhost:9091",
    )
    metrics_export_port: int = Field(
        default=8000,
        ge=1024,
        le=65535,
    )

    model_config = SettingsConfigDict(
        env_prefix="PROMETHEUS_",
        env_file=".env",
        extra="ignore",
    )


class AgentSettings(BaseSettings):
    """Behavioural settings for the AI agent decision loop."""

    poll_interval_seconds: int = Field(
        default=60,
        ge=10,
        description="How frequently the agent polls the carbon intensity signal.",
    )
    carbon_intensity_threshold_high: float = Field(
        default=250.0,
        description="gCO2eq/kWh above which the agent scales workloads down.",
    )
    carbon_intensity_threshold_low: float = Field(
        default=100.0,
        description="gCO2eq/kWh below which the agent permits full-scale workloads.",
    )
    min_replicas: int = Field(
        default=1,
        ge=0,
        description="Minimum replica count any optimization may target.",
    )
    max_replicas: int = Field(
        default=10,
        ge=1,
        description="Maximum replica count any optimization may target.",
    )
    cpu_safety_threshold: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="CPU request ratio above which scale-down is unsafe.",
    )
    latency_sla_threshold_seconds: float = Field(
        default=1.0,
        gt=0.0,
        description="P99 latency threshold used by recommendation and validation policies.",
    )
    max_scale_down_percentage: float = Field(
        default=0.50,
        gt=0.0,
        le=1.0,
        description="Maximum per-action replica reduction without review.",
    )
    optimization_cooldown_seconds: int = Field(
        default=900,
        ge=0,
        description="Minimum time between optimization actions.",
    )
    max_carbon_data_age_seconds: int = Field(
        default=600,
        ge=0,
        description="Maximum acceptable carbon data age for optimization decisions.",
    )

    @field_validator("carbon_intensity_threshold_low")
    @classmethod
    def low_must_be_less_than_high(cls, v: float, info: object) -> float:  # noqa: ANN001
        # Validated after high is known
        return v

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )


class ReportingSettings(BaseSettings):
    """Settings for the weekly GreenOps report."""

    schedule_cron: str = Field(
        default="0 8 * * MON",
        description="APScheduler-compatible cron expression.",
    )
    output_dir: str = Field(default="./reports/output")
    recipients: str = Field(
        default="",
        description="Comma-separated list of email recipients.",
    )
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_use_tls: bool = Field(default=True)

    model_config = SettingsConfigDict(
        env_prefix="REPORT_",
        env_file=".env",
        extra="ignore",
    )


class Settings:
    """
    Aggregated settings facade.

    Instantiate once via ``get_settings()`` and inject where needed.
    """

    def __init__(self) -> None:
        self.app = AppSettings()
        self.electricity_maps = ElectricityMapsSettings()
        self.kubernetes = KubernetesSettings()
        self.prometheus = PrometheusSettings()
        self.agent = AgentSettings()
        self.reporting = ReportingSettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance."""
    return Settings()
