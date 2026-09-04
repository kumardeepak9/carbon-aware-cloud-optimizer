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

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ELECTRICITY_MAPS_ZONE_PATTERN = re.compile(r"^[A-Z0-9]{2,}(?:-[A-Z0-9]+)*$")
_GIT_REF_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Values that mean "this was never filled in". A placeholder secret must never
# reach a real credential check — reject it eagerly so misconfiguration fails
# at startup rather than at the first authenticated request.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "changeme",
        "your-api-key-here",
        "your-github-token-here",
        "your-github-token",
        "your-token-here",
        "replace-me",
        "todo",
        "xxx",
    }
)


def _looks_like_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_SECRETS


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


def _validate_electricity_maps_zone(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ELECTRICITY_MAPS_ZONE must be a string")
    zone = value.strip().upper()
    if not zone:
        raise ValueError("ELECTRICITY_MAPS_ZONE must not be empty")
    if len(zone) > 64:
        raise ValueError("ELECTRICITY_MAPS_ZONE is too long")
    if not _ELECTRICITY_MAPS_ZONE_PATTERN.fullmatch(zone):
        raise ValueError("ELECTRICITY_MAPS_ZONE must contain only letters, numbers, and hyphens")
    return zone


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

    api_key: SecretStr = Field(
        description="Electricity Maps API key — REQUIRED, set via ELECTRICITY_MAPS_API_KEY.",
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

    @field_validator("zone", mode="before")
    @classmethod
    def zone_must_be_valid(cls, v: object) -> str:
        return _validate_electricity_maps_zone(v)

    @field_validator("api_key")
    @classmethod
    def api_key_must_be_real(cls, v: SecretStr) -> SecretStr:
        if _looks_like_placeholder(v.get_secret_value()):
            raise ValueError(
                "ELECTRICITY_MAPS_API_KEY is unset or still a placeholder — set a real key"
            )
        return v

    @field_validator("base_url")
    @classmethod
    def base_url_must_be_https(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("ELECTRICITY_MAPS_BASE_URL must be an https:// URL")
        return v.rstrip("/")

    model_config = SettingsConfigDict(
        env_prefix="ELECTRICITY_MAPS_",
        env_file=".env",
        extra="ignore",
    )


class KubernetesSettings(BaseSettings):
    """Kubernetes context for read-only observation.

    GreenOps has NO Kubernetes API client and never writes to a cluster — the
    only path to Kubernetes is Git -> Argo CD. This block therefore holds just
    the namespace used to scope Prometheus queries; there is deliberately no
    kubeconfig / in-cluster credential field.
    """

    namespace: str = Field(
        default="greenops",
        description="Namespace used to scope Prometheus queries for the workload.",
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
        description="Carbon-exporter fetch interval (Electricity Maps poll cadence).",
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

    @model_validator(mode="after")
    def replica_bounds_are_consistent(self) -> AgentSettings:
        if self.min_replicas > self.max_replicas:
            raise ValueError("AGENT_MIN_REPLICAS must be less than or equal to AGENT_MAX_REPLICAS")
        return self

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )


class GitOpsSettings(BaseSettings):
    """Settings for review-first GitHub GitOps change preparation."""

    repo_path: Path = Field(
        default=Path("."),
        description="Local checkout containing Kubernetes desired-state files.",
    )
    base_branch: str = Field(
        default="main",
        description="Base branch for review-first GreenOps pull requests.",
    )
    branch_prefix: str = Field(
        default="greenops",
        description="Prefix for dedicated GreenOps optimization branches.",
    )
    manifest_path: Path = Field(
        default=Path("k8s/overlays/prod/kustomization.yaml"),
        description="Only this desired-state file may be modified by Phase 8.",
    )
    deployment_name: str = Field(
        default="greenops-demo-workload",
        description="Deployment whose replica patch may be updated.",
    )
    github_repository: str | None = Field(
        default=None,
        description="GitHub repository in owner/name format.",
    )
    github_api_url: str = Field(
        default="https://api.github.com",
        description="GitHub API base URL.",
    )
    github_token: SecretStr | None = Field(
        default=None,
        description="GitHub token sourced only from GREENOPS_GITOPS_GITHUB_TOKEN.",
    )
    create_pull_request: bool = Field(
        default=False,
        description="When true, call the GitHub API. Otherwise prepare PR metadata only.",
    )

    @model_validator(mode="after")
    def gitops_settings_are_safe(self) -> GitOpsSettings:
        if not _GIT_REF_COMPONENT_PATTERN.fullmatch(self.base_branch):
            raise ValueError(
                "GREENOPS_GITOPS_BASE_BRANCH must be a simple git branch name "
                "containing only letters, numbers, '.', '_', '-', and '/'"
            )
        if ".." in self.base_branch or self.base_branch.endswith(("/", ".")):
            raise ValueError("GREENOPS_GITOPS_BASE_BRANCH is not a safe git branch name")
        if not _GIT_REF_COMPONENT_PATTERN.fullmatch(self.branch_prefix):
            raise ValueError(
                "GREENOPS_GITOPS_BRANCH_PREFIX must contain only letters, numbers, "
                "'.', '_', '-', and '/' and must not start with '-'"
            )
        if ".." in self.branch_prefix or self.branch_prefix.endswith(("/", ".")):
            raise ValueError("GREENOPS_GITOPS_BRANCH_PREFIX is not a safe git branch prefix")
        if self.manifest_path.is_absolute():
            raise ValueError("GREENOPS_GITOPS_MANIFEST_PATH must be relative to repo_path")
        if ".." in self.manifest_path.parts:
            raise ValueError("GREENOPS_GITOPS_MANIFEST_PATH must not traverse parent directories")
        if not self.manifest_path.as_posix().startswith("k8s/"):
            raise ValueError("GREENOPS_GITOPS_MANIFEST_PATH must point under k8s/")
        if self.create_pull_request and not self.github_repository:
            raise ValueError(
                "GREENOPS_GITOPS_GITHUB_REPOSITORY is required when PR creation is enabled"
            )
        if self.github_repository and not _GITHUB_REPOSITORY_PATTERN.fullmatch(
            self.github_repository
        ):
            raise ValueError("GREENOPS_GITOPS_GITHUB_REPOSITORY must be in owner/name format")
        parsed_api_url = urlparse(self.github_api_url)
        if parsed_api_url.scheme != "https" or not parsed_api_url.netloc:
            raise ValueError("GREENOPS_GITOPS_GITHUB_API_URL must be an HTTPS URL")
        if parsed_api_url.username or parsed_api_url.password:
            raise ValueError("GREENOPS_GITOPS_GITHUB_API_URL must not contain credentials")
        if self.create_pull_request and self.github_token is None:
            raise ValueError("GREENOPS_GITOPS_GITHUB_TOKEN is required when PR creation is enabled")
        return self

    model_config = SettingsConfigDict(
        env_prefix="GREENOPS_GITOPS_",
        env_file=".env",
        extra="ignore",
    )


class ReportingSettings(BaseSettings):
    """Settings for the weekly GreenOps report (`python -m reports.report`)."""

    output_dir: str = Field(
        default="./reports/output",
        description="Directory the generated weekly Markdown report is written to.",
    )
    decision_history_path: str = Field(
        default="./reports/decision-history.jsonl",
        description=(
            "Append-only JSONL log of completed optimization lifecycles. "
            "Read by the weekly report and the chat query interface; the chat "
            "interface answers historical questions only from these records."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="REPORT_",
        env_file=".env",
        extra="ignore",
    )


class ConfigurationError(RuntimeError):
    """Raised at startup when configuration is missing or unsafe for the environment."""


def _environment_safety_problems(
    app: AppSettings, prometheus: PrometheusSettings, gitops: GitOpsSettings
) -> list[str]:
    """Cross-cutting checks that are only unsafe *because* APP_ENV=production.

    Each sub-model already validates its own fields; this catches the
    dev-default-in-production combinations that no single model can see.
    """
    if app.app_env != "production":
        return []
    problems: list[str] = []
    if app.log_format != "json":
        problems.append("LOG_FORMAT must be 'json' in production (structured logs)")
    if _is_loopback_url(prometheus.api_url):
        problems.append(
            f"PROMETHEUS_API_URL points at a loopback address ({prometheus.api_url}) "
            "— set the real Prometheus endpoint"
        )
    if gitops.create_pull_request:
        token = gitops.github_token
        if token is None or _looks_like_placeholder(token.get_secret_value()):
            problems.append(
                "GREENOPS_GITOPS_GITHUB_TOKEN is unset or a placeholder while "
                "GREENOPS_GITOPS_CREATE_PULL_REQUEST=true"
            )
    return problems


def assert_safe_for_environment() -> None:
    """Fail fast if dev-only configuration is active while APP_ENV=production.

    Call from every process entry point. Only reads APP_ENV / LOG_FORMAT /
    Prometheus / GitOps — it does NOT require the Electricity Maps key, so the
    read-only agent, health, gitops and chat CLIs can call it safely.
    """
    problems = _environment_safety_problems(AppSettings(), PrometheusSettings(), GitOpsSettings())
    if problems:
        raise ConfigurationError(
            "Unsafe configuration for APP_ENV=production:\n  - " + "\n  - ".join(problems)
        )


class Settings:
    """
    Aggregated settings facade.

    Instantiate once via ``get_settings()`` and inject where needed. Construction
    fails fast: a missing required variable raises ``pydantic.ValidationError``
    from the relevant sub-model, and an unsafe production combination raises
    ``ConfigurationError``.
    """

    def __init__(self) -> None:
        self.app = AppSettings()
        # api_key comes from ELECTRICITY_MAPS_API_KEY at runtime, not a kwarg.
        self.electricity_maps = ElectricityMapsSettings()  # type: ignore[call-arg]
        self.kubernetes = KubernetesSettings()
        self.prometheus = PrometheusSettings()
        self.agent = AgentSettings()
        self.gitops = GitOpsSettings()
        self.reporting = ReportingSettings()
        problems = _environment_safety_problems(self.app, self.prometheus, self.gitops)
        if problems:
            raise ConfigurationError(
                "Unsafe configuration for APP_ENV=production:\n  - " + "\n  - ".join(problems)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance (constructed once, fails fast)."""
    return Settings()
