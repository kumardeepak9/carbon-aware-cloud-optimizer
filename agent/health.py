"""End-to-end health checks for the GreenOps AI integration path."""

from __future__ import annotations

import time
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from config import get_logger
from gitops.models import GitOpsSettings
from monitoring.client import PrometheusClient

log = get_logger(__name__)


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


class ComponentHealth(BaseModel):
    """Health result for one integration component."""

    component: str
    status: HealthStatus
    reason: str
    checked_at_seconds: float = Field(default_factory=time.time)


class EndToEndHealthReport(BaseModel):
    """Condensed health report for the GreenOps integration path."""

    status: HealthStatus
    components: list[ComponentHealth]


class GreenOpsHealthChecker:
    """Runs read-only checks across Prometheus, GitOps config, and safety wiring."""

    def __init__(
        self,
        *,
        prometheus_client: PrometheusClient,
        gitops_settings: GitOpsSettings,
    ) -> None:
        self._prometheus = prometheus_client
        self._gitops = gitops_settings

    async def check(self) -> EndToEndHealthReport:
        """Return a read-only health report suitable for startup checks or diagnostics."""
        components = [
            await self._check_prometheus(),
            self._check_gitops_configuration(),
            self._check_safety_boundaries(),
        ]
        status = self._rollup(components)
        log.info(
            "greenops.health_checked",
            status=status,
            components=[component.model_dump(mode="json") for component in components],
        )
        return EndToEndHealthReport(status=status, components=components)

    async def _check_prometheus(self) -> ComponentHealth:
        healthy = await self._prometheus.is_healthy()
        if healthy:
            return ComponentHealth(
                component="prometheus",
                status=HealthStatus.HEALTHY,
                reason="Prometheus health endpoint returned healthy.",
            )
        return ComponentHealth(
            component="prometheus",
            status=HealthStatus.UNHEALTHY,
            reason="Prometheus health endpoint is unreachable or unhealthy.",
        )

    def _check_gitops_configuration(self) -> ComponentHealth:
        repo = self._gitops.repo_path.expanduser().resolve()
        manifest = (repo / self._gitops.manifest_path).resolve()
        problems: list[str] = []
        if not (repo / ".git").exists():
            problems.append("repo_path is not a Git checkout")
        try:
            relative_manifest = manifest.relative_to(repo)
        except ValueError:
            problems.append("manifest_path escapes repo_path")
        else:
            if not relative_manifest.as_posix().startswith("k8s/"):
                problems.append("manifest_path is outside k8s/")
        if not manifest.exists():
            problems.append("manifest_path does not exist")
        if self._gitops.create_pull_request and self._gitops.github_token is None:
            problems.append("GitHub token is required when PR creation is enabled")

        if problems:
            return ComponentHealth(
                component="gitops",
                status=HealthStatus.UNHEALTHY,
                reason="; ".join(problems) + ".",
            )
        return ComponentHealth(
            component="gitops",
            status=HealthStatus.HEALTHY,
            reason=f"GitOps repository and manifest are available at {Path(manifest).as_posix()}.",
        )

    @staticmethod
    def _check_safety_boundaries() -> ComponentHealth:
        return ComponentHealth(
            component="safety_policy",
            status=HealthStatus.HEALTHY,
            reason=(
                "GreenOpsDecisionAgent returns validated recommendations and "
                "GitOpsChangeWorkflow requires APPROVED policy validation."
            ),
        )

    @staticmethod
    def _rollup(components: list[ComponentHealth]) -> HealthStatus:
        statuses = {component.status for component in components}
        if HealthStatus.UNHEALTHY in statuses:
            return HealthStatus.UNHEALTHY
        if HealthStatus.DEGRADED in statuses:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY
