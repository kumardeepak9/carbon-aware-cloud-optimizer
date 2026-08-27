# gitops

Review-first GitHub GitOps integration for approved GreenOps workload changes.

## Safety contract

`GitOpsChangeWorkflow` consumes a `ValidatedRecommendation`. It prepares a
change only when Phase 7 returns `APPROVED` and
`approved_for_gitops_change=true`.

The workflow:

- creates a dedicated `greenops/...` branch
- modifies only the configured Kubernetes desired-state manifest under `k8s/`
- updates only the Kustomize `/spec/replicas` patch for the configured Deployment
- creates a commit containing the AI decision and policy validation metadata
- prepares PR title/body, or creates the PR when explicitly enabled
- never calls Kubernetes, Argo CD, or production infrastructure APIs

## Configuration

All credentials and repository options come from environment variables:

| Variable | Purpose |
|---|---|
| `GREENOPS_GITOPS_REPO_PATH` | Local desired-state repository checkout |
| `GREENOPS_GITOPS_BASE_BRANCH` | Pull request base branch |
| `GREENOPS_GITOPS_BRANCH_PREFIX` | Dedicated branch prefix |
| `GREENOPS_GITOPS_MANIFEST_PATH` | Allowed Kubernetes manifest path |
| `GREENOPS_GITOPS_DEPLOYMENT_NAME` | Deployment whose replica patch may change |
| `GREENOPS_GITOPS_GITHUB_REPOSITORY` | GitHub repository in `owner/name` format |
| `GREENOPS_GITOPS_GITHUB_TOKEN` | GitHub token, never hardcoded |
| `GREENOPS_GITOPS_CREATE_PULL_REQUEST` | Set true to call the GitHub API |

Run one preparation cycle with:

```bash
make gitops
```
