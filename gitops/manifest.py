"""Narrow Kubernetes desired-state edits for GreenOps GitOps changes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplicaPatchUpdate:
    """Result of updating one Kustomize replica patch."""

    content: str
    previous_replicas: int
    new_replicas: int
    changed: bool


def update_kustomize_replica_patch(
    content: str,
    *,
    deployment_name: str,
    replicas: int,
) -> ReplicaPatchUpdate:
    """Update only the JSON6902 patch value for /spec/replicas on one Deployment."""
    lines = content.splitlines(keepends=True)
    target_start: int | None = None
    patch_start: int | None = None
    replica_path_seen = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "- target:":
            target_start = index
            patch_start = None
            replica_path_seen = False
            continue

        if target_start is None:
            continue

        if index > target_start and stripped == "- target:":
            target_start = index
            patch_start = None
            replica_path_seen = False
            continue

        if stripped.startswith("name:") and stripped.split(":", 1)[1].strip() != deployment_name:
            target_start = None
            patch_start = None
            replica_path_seen = False
            continue

        if stripped == "patch: |-":
            patch_start = index
            continue

        if patch_start is not None and stripped == "path: /spec/replicas":
            replica_path_seen = True
            continue

        if patch_start is not None and replica_path_seen and stripped.startswith("value:"):
            prefix = line[: line.index("value:")]
            newline = "\n" if line.endswith("\n") else ""
            raw_value = stripped.split(":", 1)[1].strip()
            previous = int(raw_value)
            replacement = f"{prefix}value: {replicas}{newline}"
            new_lines = [*lines]
            new_lines[index] = replacement
            return ReplicaPatchUpdate(
                content="".join(new_lines),
                previous_replicas=previous,
                new_replicas=replicas,
                changed=previous != replicas,
            )

    raise ValueError(
        "Could not find a Kustomize /spec/replicas patch for Deployment "
        f"{deployment_name!r}."
    )
