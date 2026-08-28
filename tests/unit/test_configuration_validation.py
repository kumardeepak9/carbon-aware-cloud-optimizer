"""Tests for GreenOps production configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import AgentSettings, GitOpsSettings


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
