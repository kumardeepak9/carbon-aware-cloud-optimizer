"""Tests for the Phase 8 review-first GitOps workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response
from pydantic import SecretStr

from agent.models import ValidatedRecommendation, ValidationStatus
from agent.policy import DecisionPolicy
from agent.safety import OptimizationSafetyPolicy
from gitops.github import GitHubClient, PullRequestResult
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
    def __init__(
        self,
        result: PullRequestResult | None = None,
        *,
        creates_pull_requests: bool = False,
    ) -> None:
        self.result = result or PullRequestResult(created=False, prepared=True)
        self.creates_pull_requests = creates_pull_requests
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


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "master")
    return repo, remote


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


@pytest.mark.asyncio
async def test_pushes_branch_before_creating_pull_request_when_enabled(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    validated = _approved_recommendation()
    github = FakeGitHubClient(
        PullRequestResult(
            created=True,
            prepared=True,
            url="https://github.example/pull/1",
        ),
        creates_pull_requests=True,
    )
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(
            repo_path=repo,
            base_branch="master",
            create_pull_request=True,
            github_repository="example/repo",
            github_token=SecretStr("ghp_test_token"),
        ),
        github=github,
    )

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.PR_CREATED
    assert result.pull_request_url == "https://github.example/pull/1"
    assert _git(remote, "rev-parse", result.branch_name) == result.commit_sha
    assert github.calls[0]["head"] == result.branch_name


@pytest.mark.asyncio
async def test_existing_greenops_branch_with_unrelated_commit_is_blocked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    validated = _approved_recommendation()
    branch_name = "greenops/scale-down-greenops-demo-workload-to-2-low-load-high-carbon"
    _git(repo, "switch", "-c", branch_name)
    (repo / "k8s" / "overlays" / "prod" / "extra.yaml").write_text(
        "kind: ConfigMap\n",
        encoding="utf-8",
    )
    _git(repo, "add", "--", "k8s/overlays/prod/extra.yaml")
    _git(repo, "commit", "-m", "unrelated k8s change")
    _git(repo, "switch", "master")
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(repo_path=repo, base_branch="master"),
        github=FakeGitHubClient(),
    )

    result = await workflow.prepare_change(validated)

    assert result.status is GitOpsChangeStatus.BLOCKED
    assert "outside the allowed manifest" in result.reason
    assert "k8s/overlays/prod/extra.yaml" in result.changed_files


@pytest.mark.asyncio
async def test_rejected_policy_decision_does_not_create_github_call(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    recommendation = DecisionPolicy().recommend(
        _observation(carbon_data_available=0.0),
        now=1_000.0,
    )
    validation = OptimizationSafetyPolicy().validate(recommendation, now=1_000.0)
    github = FakeGitHubClient(creates_pull_requests=True)
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(
            repo_path=repo,
            base_branch="master",
            create_pull_request=True,
            github_repository="example/repo",
            github_token=SecretStr("ghp_test_token"),
        ),
        github=github,
    )

    result = await workflow.prepare_change(
        ValidatedRecommendation(recommendation=recommendation, validation=validation)
    )

    assert result.status is GitOpsChangeStatus.BLOCKED
    assert github.calls == []


@pytest.mark.asyncio
async def test_github_token_is_redacted_from_workflow_logs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    validated = _approved_recommendation()
    secret = "ghp_super_secret"
    github = FakeGitHubClient(
        PullRequestResult(
            created=False,
            prepared=True,
            error=f"bad credentials {secret}",
        )
    )
    workflow = GitOpsChangeWorkflow(
        GitOpsSettings(
            repo_path=repo,
            base_branch="master",
            github_token=SecretStr(secret),
        ),
        github=github,
    )

    with patch("gitops.workflow.log.warning") as warning:
        result = await workflow.prepare_change(validated)

    logged = " ".join(
        [str(arg) for call in warning.call_args_list for arg in call.args]
        + [
            str(value)
            for call in warning.call_args_list
            for value in call.kwargs.values()
        ]
    )
    assert result.status is GitOpsChangeStatus.PR_FAILED
    assert secret not in logged
    assert "[redacted]" in logged


@pytest.mark.asyncio
@respx.mock
async def test_github_client_creates_pull_request_with_token_header() -> None:
    route = respx.post("https://api.github.test/repos/example/repo/pulls").mock(
        return_value=Response(201, json={"html_url": "https://github.test/pr/1"})
    )
    client = GitHubClient(
        repository="example/repo",
        api_url="https://api.github.test",
        token=SecretStr("ghp_secret"),
        create_pull_request=True,
    )

    result = await client.create_or_prepare_pull_request(
        title="title",
        body="body",
        head="greenops/change",
        base="main",
    )

    assert result.created is True
    assert result.url == "https://github.test/pr/1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer ghp_secret"


@pytest.mark.asyncio
@respx.mock
async def test_github_client_returns_existing_pull_request_on_duplicate() -> None:
    respx.post("https://api.github.test/repos/example/repo/pulls").mock(
        return_value=Response(422, json={"message": "Validation Failed"})
    )
    respx.get("https://api.github.test/repos/example/repo/pulls").mock(
        return_value=Response(200, json=[{"html_url": "https://github.test/pr/existing"}])
    )
    client = GitHubClient(
        repository="example/repo",
        api_url="https://api.github.test",
        token=SecretStr("ghp_secret"),
        create_pull_request=True,
    )

    result = await client.create_or_prepare_pull_request(
        title="title",
        body="body",
        head="greenops/change",
        base="main",
    )

    assert result.created is False
    assert result.url == "https://github.test/pr/existing"


@pytest.mark.asyncio
@respx.mock
async def test_github_client_redacts_token_from_api_errors() -> None:
    secret = "ghp_secret"
    respx.post("https://api.github.test/repos/example/repo/pulls").mock(
        return_value=Response(401, json={"message": f"bad token {secret}"})
    )
    client = GitHubClient(
        repository="example/repo",
        api_url="https://api.github.test",
        token=SecretStr(secret),
        create_pull_request=True,
    )

    result = await client.create_or_prepare_pull_request(
        title="title",
        body="body",
        head="greenops/change",
        base="main",
    )

    assert result.error is not None
    assert secret not in result.error
    assert "[redacted]" in result.error
