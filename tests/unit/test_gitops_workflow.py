"""Tests for the Phase 8 review-first GitOps workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.models import ValidatedRecommendation, ValidationStatus
from agent.policy import DecisionPolicy
from agent.safety import OptimizationSafetyPolicy
from gitops.github import PullRequestResult
from gitops.manifest import update_kustomize_replica_patch
from gitops.models import GitOpsChangeStatus, GitOpsSettings
from gitops.workflow import GitOpsChangeWorkflow
from tests.unit.test_decision_policy import _observation


KUSTOMIZATION = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
patches:
  - target:
      kind: Deployment
      name: greenops-demo-workload
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 3
  - target:
      kind: ConfigMap
      name: greenops-demo-workload-config
    patch: |-
      - op: replace
        path: /data/LOG_LEVEL
        value: "INFO"
"""


class FakeGitHubClient:
    def __init__(self, result: PullRequestResult | None = None) -> None:
        self.result = result or PullRequestResult(created=False, prepared=True)
        self.calls: list[dict[str, str]] = []

    async def create_or_prepare_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> PullRequestResult:
        self.calls.append({"title": title, "body": body, "head": head, "base": base})
        return self.result


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    manifest = repo / "k8s" / "overlays" / "prod" / "kustomization.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(KUSTOMIZATION, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "greenops@example.test")
    _git(repo, "config", "user.name", "GreenOps Bot")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-m", "initial desired state")
    return repo


def _approved_recommendation() -> ValidatedRecommendation:
    recommendation = DecisionPolicy().recommend(_observation(), now=1_000.0)
    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)
    assert validation.status is ValidationStatus.APPROVED
    return ValidatedRecommendation(
        recommendation=recommendation.model_copy(update={"recommended_replicas": 2}),
        validation=validation,
    )


def test_manifest_update_only_changes_deployment_replica_patch() -> None:
    update = update_kustomize_replica_patch(
        KUSTOMIZATION,
        deployment_name="greenops-demo-workload",
        replicas=2,
    )

    assert update.previous_replicas == 3
    assert update.new_replicas == 2
    assert update.changed is True
    assert 'value: "INFO"' in update.content
    assert "value: 2" in update.content


@pytest.mark.asyncio
async def test_prepares_branch_commit_and_pr_metadata_for_approved_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    validated = _approved_recommendation()
    github = FakeGitHubClient()
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(repo_path=repo, base_branch="master"),
        github=github,
    )

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.PREPARED
    assert result.branch_name == "greenops/scale-down-greenops-demo-workload-to-2-low-load-high-carbon"
    assert result.changed_files == ["k8s/overlays/prod/kustomization.yaml"]
    assert result.commit_sha is not None
    assert result.pull_request_title is not None
    assert result.pull_request_body is not None
    assert len(github.calls) == 1
    assert "Policy validation" in github.calls[0]["body"]
    assert "value: 2" in (repo / "k8s/overlays/prod/kustomization.yaml").read_text()


@pytest.mark.asyncio
async def test_blocks_when_policy_validation_is_not_approved(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    recommendation = DecisionPolicy().recommend(_observation(carbon_data_available=0.0), now=1_000.0)
    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)
    workflow = GitOpsChangeWorkflow(GitOpsSettings(repo_path=repo, base_branch="master"))

    result = await workflow.prepare_change(
        ValidatedRecommendation(recommendation=recommendation, validation=validation)
    )

    assert result.status is GitOpsChangeStatus.BLOCKED
    assert "did not approve" in result.reason


@pytest.mark.asyncio
async def test_detects_no_op_without_creating_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    validated = _approved_recommendation()
    validated = validated.model_copy(
        update={
            "recommendation": validated.recommendation.model_copy(
                update={"recommended_replicas": 3}
            )
        }
    )
    workflow = GitOpsChangeWorkflow(GitOpsSettings(repo_path=repo, base_branch="master"))

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.NO_OP
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.asyncio
async def test_protects_unrelated_dirty_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("unrelated work\n", encoding="utf-8")
    validated = _approved_recommendation()
    workflow = GitOpsChangeWorkflow(GitOpsSettings(repo_path=repo, base_branch="master"))

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.BLOCKED
    assert "uncommitted changes" in result.reason


@pytest.mark.asyncio
async def test_github_api_failure_returns_safe_result_after_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    validated = _approved_recommendation()
    github = FakeGitHubClient(PullRequestResult(created=False, prepared=True, error="boom"))
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(repo_path=repo, base_branch="master"),
        github=github,
    )

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.PR_FAILED
    assert result.commit_sha is not None
    assert result.pull_request_url is None
    assert "pull request creation failed" in result.reason
