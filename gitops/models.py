"""Models and settings for review-first GitOps changes."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from config.settings import GitOpsSettings


class GitOpsChangeStatus(StrEnum):
    BLOCKED = "BLOCKED"
    NO_OP = "NO_OP"
    PREPARED = "PREPARED"
    PR_CREATED = "PR_CREATED"
    PR_FAILED = "PR_FAILED"
    FAILED = "FAILED"


class GitOpsChangeResult(BaseModel):
    """Auditable outcome of a GitOps preparation attempt."""

    status: GitOpsChangeStatus
    reason: str
    branch_name: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None
    pull_request_title: str | None = None
    pull_request_body: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    audit_metadata: dict[str, object] = Field(default_factory=dict)
