"""Static security checks for container and local-compose configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def test_runtime_dockerfiles_do_not_install_curl_for_healthchecks() -> None:
    for dockerfile in (REPO / "docker").glob("*.Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        runtime_stage = text.split("FROM python:3.11-slim AS runtime", maxsplit=1)[1]
        assert "apt-get install" not in runtime_stage
        assert "curl " not in runtime_stage
        assert "HEALTHCHECK" in runtime_stage


def test_runtime_dockerfiles_run_as_non_root() -> None:
    for dockerfile in (REPO / "docker").glob("*.Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        assert "USER 1001" in text


def test_compose_app_containers_drop_privileges_and_use_read_only_filesystems() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    for service_name in ("workload", "carbon-exporter"):
        service = compose["services"][service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]


def test_compose_observability_services_drop_privileges() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    for service_name in ("prometheus", "pushgateway", "grafana"):
        service = compose["services"][service_name]
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]


def test_compose_prometheus_lifecycle_api_is_not_enabled() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "--web.enable-lifecycle" not in compose["services"]["prometheus"]["command"]


def test_compose_grafana_requires_non_default_admin_password() -> None:
    compose_text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GRAFANA_ADMIN_PASSWORD:-changeme" not in compose_text
    assert "admin / changeme" not in compose_text
    assert "GF_FEATURE_TOGGLES_ENABLE=publicDashboards" not in compose_text
    assert "GRAFANA_ADMIN_PASSWORD:?" in compose_text
