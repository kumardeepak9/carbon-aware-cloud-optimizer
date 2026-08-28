"""Review-first GitOps workflow for approved GreenOps decisions."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

from agent.models import Action, ValidatedRecommendation, ValidationStatus
from config import get_logger
from gitops.github import GitHubClient
from gitops.manifest import update_kustomize_replica_patch
from gitops.models import GitOpsChangeResult, GitOpsChangeStatus, GitOpsSettings

log = get_logger(__name__)


class GitOpsChangeWorkflow:
    """Prepares a controlled Git branch, commit, and pull request metadata."""

    def __init__(
        self,
        settings: GitOpsSettings | None = None,
        github: GitHubClient | None = None,
    ) -> None:
        self.settings = settings or GitOpsSettings()
        self.github = github or GitHubClient(
            repository=self.settings.github_repository,
            api_url=self.settings.github_api_url,
            token=self.settings.github_token,
            create_pull_request=self.settings.create_pull_request,
        )

    async def prepare_change(
        self,
        validated: ValidatedRecommendation,
    ) -> GitOpsChangeResult:
        """Prepare a GitOps change only when Phase 7 validation approves it."""
        audit = self._audit_metadata(validated)
        try:
            return await self._prepare_change(validated, audit)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            log.error(
                "gitops_change_failed",
                error=str(exc),
                **audit,
            )
            return self._result(
                GitOpsChangeStatus.FAILED,
                f"GitOps change preparation failed safely: {exc}",
                audit,
            )

    async def _prepare_change(
        self,
        validated: ValidatedRecommendation,
        audit: dict[str, object],
    ) -> GitOpsChangeResult:
        recommendation = validated.recommendation
        validation = validated.validation

        if validation.status is not ValidationStatus.APPROVED:
            return self._blocked(
                f"Policy validation did not approve the recommendation: {validation.reason}",
                audit,
            )
        if not validation.approved_for_gitops_change:
            return self._blocked(
                "Policy validation was approved for observation only, not a GitOps change.",
                audit,
            )
        if recommendation.action not in {Action.SCALE_DOWN, Action.SCALE_UP}:
            return self._blocked(
                f"Action {recommendation.action} does not require a GitOps workload change.",
                audit,
            )
        if recommendation.current_replicas is None or recommendation.recommended_replicas is None:
            return self._blocked("Replica counts are required for GitOps workload changes.", audit)
        if recommendation.current_replicas == recommendation.recommended_replicas:
            return self._result(
                GitOpsChangeStatus.NO_OP,
                "Recommended replica count already matches the current replica count.",
                audit,
            )

        repo = self._repo_path()
        manifest = self._manifest_path(repo)
        relative_manifest = manifest.relative_to(repo).as_posix()
        self._assert_allowed_manifest(relative_manifest)

        dirty_files = self._dirty_files(repo)
        if dirty_files:
            return self._blocked(
                "Repository has uncommitted changes; refusing to mix GreenOps changes with "
                f"existing work: {', '.join(dirty_files)}",
                audit,
            )

        branch_name = self._branch_name(validated)
        branch_already_exists = self._branch_exists(repo, branch_name)
        if not branch_already_exists:
            content = manifest.read_text(encoding="utf-8")
            patch_update = update_kustomize_replica_patch(
                content,
                deployment_name=self.settings.deployment_name,
                replicas=recommendation.recommended_replicas,
            )
            if not patch_update.changed:
                return self._result(
                    GitOpsChangeStatus.NO_OP,
                    "Desired-state manifest already contains the recommended replica count.",
                    audit | {"manifest_path": relative_manifest},
                    changed_files=[],
                    manifest_path=relative_manifest,
                )

        self._checkout_branch(repo, branch_name)

        content = manifest.read_text(encoding="utf-8")
        patch_update = update_kustomize_replica_patch(
            content,
            deployment_name=self.settings.deployment_name,
            replicas=recommendation.recommended_replicas,
        )
        if not patch_update.changed:
            status = GitOpsChangeStatus.PREPARED if branch_already_exists else GitOpsChangeStatus.NO_OP
            reason = (
                "Dedicated GitOps branch already contains the recommended replica count."
                if branch_already_exists
                else "Desired-state manifest already contains the recommended replica count."
            )
            return self._result(
                status,
                reason,
                audit | {"manifest_path": relative_manifest},
                branch_name=branch_name if branch_already_exists else None,
                commit_sha=self._git(repo, "rev-parse", "HEAD").stdout.strip()
                if branch_already_exists
                else None,
                changed_files=[],
                manifest_path=relative_manifest,
            )

        manifest.write_text(patch_update.content, encoding="utf-8")

        changed_files = self._changed_files(repo)
        if changed_files != [relative_manifest]:
            return self._blocked(
                "GitOps workflow attempted to modify files outside the allowed manifest: "
                + ", ".join(changed_files),
                audit,
                branch_name=branch_name,
                changed_files=changed_files,
                manifest_path=relative_manifest,
            )

        commit_message = self._commit_message(validated, relative_manifest)
        self._git(repo, "add", "--", relative_manifest)
        self._git(repo, "commit", "-m", commit_message)
        commit_sha = self._git(repo, "rev-parse", "HEAD").stdout.strip()
        pr_title = self._pull_request_title(validated)
        pr_body = self._pull_request_body(validated, relative_manifest, commit_sha)
        pr_result = await self.github.create_or_prepare_pull_request(
            title=pr_title,
            body=pr_body,
            head=branch_name,
            base=self.settings.base_branch,
        )

        if pr_result.error:
            log.warning(
                "gitops_pull_request_failed",
                branch_name=branch_name,
                commit_sha=commit_sha,
                manifest_path=relative_manifest,
                github_error=pr_result.error,
                **audit,
            )
            return self._result(
                GitOpsChangeStatus.PR_FAILED,
                f"GitOps commit prepared, but pull request creation failed: {pr_result.error}",
                audit,
                branch_name=branch_name,
                commit_sha=commit_sha,
                changed_files=changed_files,
                manifest_path=relative_manifest,
                pull_request_title=pr_title,
                pull_request_body=pr_body,
            )

        status = GitOpsChangeStatus.PR_CREATED if pr_result.created else GitOpsChangeStatus.PREPARED
        reason = (
            "GitOps branch, commit, and pull request were created."
            if pr_result.created
            else "GitOps branch and commit prepared; pull request metadata is ready for review."
        )
        log.info(
            "gitops_change_prepared",
            status=status,
            branch_name=branch_name,
            commit_sha=commit_sha,
            manifest_path=relative_manifest,
            pull_request_url=pr_result.url,
            **audit,
        )
        return self._result(
            status,
            reason,
            audit,
            branch_name=branch_name,
            commit_sha=commit_sha,
            pull_request_url=pr_result.url,
            changed_files=changed_files,
            manifest_path=relative_manifest,
            pull_request_title=pr_title,
            pull_request_body=pr_body,
        )

    def prepare_change_sync(self, validated: ValidatedRecommendation) -> GitOpsChangeResult:
        """Synchronous wrapper for scheduler or CLI callers."""
        return asyncio.run(self.prepare_change(validated))

    def _repo_path(self) -> Path:
        repo = self.settings.repo_path.expanduser().resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"GitOps repository path is not a Git checkout: {repo}")
        return repo

    def _manifest_path(self, repo: Path) -> Path:
        manifest = (repo / self.settings.manifest_path).resolve()
        manifest.relative_to(repo)
        if not manifest.exists():
            raise FileNotFoundError(f"GitOps manifest does not exist: {manifest}")
        return manifest

    @staticmethod
    def _assert_allowed_manifest(relative_manifest: str) -> None:
        if not relative_manifest.startswith("k8s/"):
            raise ValueError("GitOps workflow may only modify files under k8s/.")

    def _dirty_files(self, repo: Path) -> list[str]:
        output = self._git(repo, "status", "--porcelain").stdout
        return sorted(line[3:] for line in output.splitlines() if line)

    def _changed_files(self, repo: Path) -> list[str]:
        output = self._git(repo, "diff", "--name-only").stdout
        return sorted(line for line in output.splitlines() if line)

    def _checkout_branch(self, repo: Path, branch_name: str) -> None:
        if self._branch_exists(repo, branch_name):
            self._git(repo, "switch", branch_name)
            return
        self._git(repo, "switch", "-c", branch_name)

    def _branch_exists(self, repo: Path, branch_name: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch_name],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _branch_name(self, validated: ValidatedRecommendation) -> str:
        recommendation = validated.recommendation
        target = recommendation.recommended_replicas
        basis = recommendation.metadata.decision_basis.replace("_", "-")
        raw = (
            f"{self.settings.branch_prefix}/"
            f"{recommendation.action.value.lower()}-"
            f"{self.settings.deployment_name}-to-{target}-{basis}"
        )
        return re.sub(r"[^a-zA-Z0-9._/-]+", "-", raw).strip("-").lower()

    @staticmethod
    def _audit_metadata(validated: ValidatedRecommendation) -> dict[str, object]:
        recommendation = validated.recommendation
        validation = validated.validation
        return {
            "agent_action": recommendation.action.value,
            "current_replicas": recommendation.current_replicas,
            "recommended_replicas": recommendation.recommended_replicas,
            "agent_reason": recommendation.reason,
            "agent_decision_basis": recommendation.metadata.decision_basis,
            "policy_validation_status": validation.status.value,
            "policy_validation_reason": validation.reason,
            "policy_version": validation.policy_version,
        }

    def _commit_message(self, validated: ValidatedRecommendation, manifest_path: str) -> str:
        recommendation = validated.recommendation
        subject = (
            "greenops: "
            f"{recommendation.action.value.lower().replace('_', '-')} "
            f"{self.settings.deployment_name} to {recommendation.recommended_replicas} replicas"
        )
        body = {
            "manifest_path": manifest_path,
            "ai_decision": recommendation.model_dump(mode="json"),
            "policy_validation": validated.validation.model_dump(mode="json"),
        }
        return subject + "\n\n" + json.dumps(body, indent=2, sort_keys=True)

    def _pull_request_title(self, validated: ValidatedRecommendation) -> str:
        recommendation = validated.recommendation
        return (
            "GreenOps: "
            f"{recommendation.action.value.replace('_', ' ').title()} "
            f"{self.settings.deployment_name} to {recommendation.recommended_replicas} replicas"
        )

    def _pull_request_body(
        self,
        validated: ValidatedRecommendation,
        manifest_path: str,
        commit_sha: str,
    ) -> str:
        recommendation = validated.recommendation
        validation = validated.validation
        return "\n".join(
            [
                "## GreenOps decision",
                "",
                f"- Action: `{recommendation.action.value}`",
                f"- Current replicas: `{recommendation.current_replicas}`",
                f"- Recommended replicas: `{recommendation.recommended_replicas}`",
                f"- Reason: {recommendation.reason}",
                "",
                "## Policy validation",
                "",
                f"- Status: `{validation.status.value}`",
                f"- Reason: {validation.reason}",
                f"- Policy version: `{validation.policy_version}`",
                "",
                "## Change",
                "",
                f"- Manifest: `{manifest_path}`",
                f"- Commit: `{commit_sha}`",
                "",
                "This pull request is review-first. It does not modify Kubernetes directly.",
            ]
        )

    def _blocked(
        self,
        reason: str,
        audit: dict[str, object],
        *,
        branch_name: str | None = None,
        changed_files: list[str] | None = None,
        manifest_path: str | None = None,
    ) -> GitOpsChangeResult:
        log.info(
            "gitops_change_blocked",
            reason=reason,
            branch_name=branch_name,
            changed_files=changed_files or [],
            manifest_path=manifest_path,
            **audit,
        )
        return self._result(
            GitOpsChangeStatus.BLOCKED,
            reason,
            audit,
            branch_name=branch_name,
            changed_files=changed_files or [],
            manifest_path=manifest_path,
        )

    def _result(
        self,
        status: GitOpsChangeStatus,
        reason: str,
        audit: dict[str, object],
        *,
        branch_name: str | None = None,
        commit_sha: str | None = None,
        pull_request_url: str | None = None,
        pull_request_title: str | None = None,
        pull_request_body: str | None = None,
        changed_files: list[str] | None = None,
        manifest_path: str | None = None,
    ) -> GitOpsChangeResult:
        return GitOpsChangeResult(
            status=status,
            reason=reason,
            branch_name=branch_name,
            base_branch=self.settings.base_branch,
            commit_sha=commit_sha,
            pull_request_url=pull_request_url,
            pull_request_title=pull_request_title,
            pull_request_body=pull_request_body,
            changed_files=changed_files or [],
            manifest_path=manifest_path,
            audit_metadata=audit,
        )
