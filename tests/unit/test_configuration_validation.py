"""Tests for GreenOps production configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import assert_safe_for_environment
from config.settings import (
    AgentSettings,
    ConfigurationError,
    ElectricityMapsSettings,
    GitOpsSettings,
    PrometheusSettings,
    ReportingSettings,
    Settings,
)

REAL_KEY = "em_live_0123456789abcdef"


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A minimally-valid production environment; individual tests break one thing."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", REAL_KEY)
    monkeypatch.setenv("PROMETHEUS_API_URL", "https://prometheus.internal:9090")
    monkeypatch.setenv("GREENOPS_GITOPS_CREATE_PULL_REQUEST", "false")
    return monkeypatch


def test_agent_settings_reject_inverted_replica_bounds() -> None:
    with pytest.raises(ValidationError):
        AgentSettings(min_replicas=6, max_replicas=3)


def test_gitops_settings_reject_manifest_outside_k8s() -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(manifest_path="README.md")


def test_gitops_settings_reject_parent_traversal() -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(manifest_path="../k8s/overlays/prod/kustomization.yaml")


def test_gitops_settings_require_credentials_when_pr_creation_enabled() -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(create_pull_request=True, github_repository="example/repo")


@pytest.mark.parametrize("branch", ["--upload-pack=sh", "../main", "main..prod", "prod/"])
def test_gitops_settings_reject_unsafe_base_branch(branch: str) -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(base_branch=branch)


@pytest.mark.parametrize("prefix", ["-bad", "../greenops", "greenops..bad", "greenops/"])
def test_gitops_settings_reject_unsafe_branch_prefix(prefix: str) -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(branch_prefix=prefix)


@pytest.mark.parametrize("repo", ["owner", "owner/repo/extra", "https://github.com/owner/repo"])
def test_gitops_settings_reject_invalid_github_repository(repo: str) -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(github_repository=repo)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com",
        "https://token@api.github.com",
        "api.github.com",
    ],
)
def test_gitops_settings_reject_unsafe_github_api_url(url: str) -> None:
    with pytest.raises(ValidationError):
        GitOpsSettings(github_api_url=url)


# ---------------------------------------------------------------------------
# Required variables fail fast
# ---------------------------------------------------------------------------


def test_electricity_maps_api_key_is_required() -> None:
    with pytest.raises(ValidationError):
        ElectricityMapsSettings()


@pytest.mark.parametrize("placeholder", ["", "your-api-key-here", "changeme", "TODO", "  xxx  "])
def test_electricity_maps_api_key_rejects_placeholders(
    placeholder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", placeholder)
    with pytest.raises(ValidationError):
        ElectricityMapsSettings()


def test_electricity_maps_api_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", REAL_KEY)
    s = ElectricityMapsSettings()
    assert REAL_KEY not in repr(s)
    assert s.api_key.get_secret_value() == REAL_KEY


@pytest.mark.parametrize("url", ["http://api.electricitymap.org/v3", "ftp://x", "not-a-url"])
def test_electricity_maps_base_url_must_be_https(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", REAL_KEY)
    monkeypatch.setenv("ELECTRICITY_MAPS_BASE_URL", url)
    with pytest.raises(ValidationError):
        ElectricityMapsSettings()


# ---------------------------------------------------------------------------
# Development defaults cannot silently become unsafe production defaults
# ---------------------------------------------------------------------------


def test_default_development_settings_construct_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", REAL_KEY)
    # every other var absent -> all dev defaults. Must NOT raise.
    settings = Settings()
    assert settings.app.app_env == "development"
    assert settings.prometheus.api_url == "http://localhost:9090"


def test_production_accepts_a_fully_valid_environment(prod_env: pytest.MonkeyPatch) -> None:
    settings = Settings()
    assert settings.app.app_env == "production"


def test_production_rejects_pretty_logs(prod_env: pytest.MonkeyPatch) -> None:
    prod_env.setenv("LOG_FORMAT", "pretty")
    with pytest.raises(ConfigurationError, match="LOG_FORMAT"):
        Settings()


def test_assert_safe_for_environment_needs_no_electricity_maps_key(
    prod_env: pytest.MonkeyPatch,
) -> None:
    # the read-only agent / gitops / chat entry points call this, and must not
    # be forced to carry the carbon-exporter's API key
    prod_env.delenv("ELECTRICITY_MAPS_API_KEY", raising=False)
    prod_env.setenv("LOG_FORMAT", "pretty")
    with pytest.raises(ConfigurationError, match="LOG_FORMAT"):
        assert_safe_for_environment()


def test_assert_safe_for_environment_passes_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOG_FORMAT", "pretty")  # fine in dev
    monkeypatch.setenv("PROMETHEUS_API_URL", "http://localhost:9090")
    assert_safe_for_environment()  # must not raise


@pytest.mark.parametrize(
    "url",
    ["http://localhost:9090", "http://127.0.0.1:9090", "http://prometheus.local"],
)
def test_production_rejects_loopback_prometheus_url(
    url: str, prod_env: pytest.MonkeyPatch
) -> None:
    prod_env.setenv("PROMETHEUS_API_URL", url)
    with pytest.raises(ConfigurationError, match="PROMETHEUS_API_URL"):
        Settings()


def test_production_rejects_placeholder_github_token_when_pr_enabled(
    prod_env: pytest.MonkeyPatch,
) -> None:
    prod_env.setenv("GREENOPS_GITOPS_CREATE_PULL_REQUEST", "true")
    prod_env.setenv("GREENOPS_GITOPS_GITHUB_REPOSITORY", "acme/carbon-aware-cloud-optimizer")
    prod_env.setenv("GREENOPS_GITOPS_GITHUB_TOKEN", "your-github-token-here")
    with pytest.raises((ConfigurationError, ValidationError)):
        Settings()


def test_production_allows_pr_creation_with_a_real_token(prod_env: pytest.MonkeyPatch) -> None:
    prod_env.setenv("GREENOPS_GITOPS_CREATE_PULL_REQUEST", "true")
    prod_env.setenv("GREENOPS_GITOPS_GITHUB_REPOSITORY", "acme/carbon-aware-cloud-optimizer")
    prod_env.setenv("GREENOPS_GITOPS_GITHUB_TOKEN", "ghp_realtokenvalue0123456789")
    Settings()  # must not raise


# ---------------------------------------------------------------------------
# Removed / dead variables
# ---------------------------------------------------------------------------


def test_dead_settings_fields_are_gone() -> None:
    assert "pushgateway_url" not in PrometheusSettings.model_fields
    assert "carbon_intensity_threshold_high" not in AgentSettings.model_fields
    assert "carbon_intensity_threshold_low" not in AgentSettings.model_fields
    for dead in ("schedule_cron", "recipients", "smtp_host", "smtp_password", "smtp_use_tls"):
        assert dead not in ReportingSettings.model_fields


def test_stale_env_vars_are_ignored_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    # an old .env may still carry removed keys; extra="ignore" must tolerate them
    monkeypatch.setenv("ELECTRICITY_MAPS_API_KEY", REAL_KEY)
    monkeypatch.setenv("PROMETHEUS_PUSHGATEWAY_URL", "http://localhost:9091")
    monkeypatch.setenv("AGENT_CARBON_INTENSITY_THRESHOLD_HIGH", "300")
    monkeypatch.setenv("REPORT_SMTP_HOST", "smtp.example.com")
    Settings()  # must not raise


# ---------------------------------------------------------------------------
# Logging is actually configured from the environment
# ---------------------------------------------------------------------------


def test_configure_logging_from_env_applies_log_format(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging as std_logging

    import structlog

    from config import configure_logging_from_env

    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging_from_env()
    assert std_logging.getLogger().level == std_logging.WARNING
    # structlog is now configured (not the lazy default)
    assert structlog.is_configured()
