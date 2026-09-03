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
