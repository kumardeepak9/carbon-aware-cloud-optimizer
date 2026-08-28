# GreenOps AI End-to-End Production Integration

This document describes the integrated GreenOps AI path implemented across the
existing modules. It does not introduce a new control plane.

## Architecture

```text
Electricity Maps
  -> carbon.CarbonMetricsServer / CarbonMetricsExporter
  -> greenops_carbon_* Prometheus metrics

Kubernetes workload and cluster metrics
  -> Prometheus recording rules
  -> monitoring.PrometheusClient

Carbon + operational context
  -> agent.GreenOpsDecisionAgent
  -> agent.DecisionPolicy
  -> agent.OptimizationSafetyPolicy
  -> gitops.GitOpsChangeWorkflow
  -> GitHub branch/commit/pull request
  -> Argo CD sync after review and merge
  -> Kubernetes desired state reconciled
  -> Prometheus post-change metrics
  -> agent.OptimizationVerifier

OptimizationLifecycle records + Prometheus summaries
  -> reports.WeeklyReportGenerator
```

## Safety Boundaries

The AI agent cannot directly change infrastructure. The only production action
path is:

1. `GreenOpsDecisionAgent` returns a `ValidatedRecommendation`.
2. `OptimizationSafetyPolicy` must return `APPROVED` and
   `approved_for_gitops_change=true`.
3. `GitOpsChangeWorkflow` prepares a Git branch and commit.
4. GitHub pull request review and merge remain the release gate.
5. Argo CD reconciles only after the Git desired state changes.

The code path does not call the Kubernetes API, Argo CD sync APIs, or production
infrastructure APIs from the agent.

## Health Checks

`GreenOpsHealthChecker` performs read-only checks for:

- Prometheus health endpoint reachability.
- GitOps repository and allowed manifest availability.
- Safety boundary wiring.

Run it with:

```bash
make health
```

## Verification

`ClosedLoopController` captures a pre-change Prometheus snapshot, prepares the
GitOps change, waits for the configured stabilization window, then collects a
post-change Prometheus snapshot. `OptimizationVerifier` classifies the result as
`SUCCESS`, `DEGRADED`, `ROLLBACK_REQUIRED`, or `INCONCLUSIVE`.

Rollback preparation also goes through GitOps and policy validation. It does not
write directly to Kubernetes.

## Weekly Reporting

`WeeklyReportGenerator` consumes completed `OptimizationLifecycle` records plus
optional Prometheus carbon and workload summaries. Missing data is surfaced as
data-quality notes; values are tagged as measured or estimated.

## Remaining Production Risks And Limitations

- Tests require Python 3.11+, but the current sandbox exposes Python 3.9.6.
- The GitOps workflow prepares branches and PRs; production merge policy,
  required reviews, and branch protection must be configured in GitHub.
- Argo CD sync behavior is observed indirectly through Prometheus metrics. A
  direct Argo CD health API integration is not implemented.
- Cooldown state is passed into the controller, but durable storage for the last
  optimization timestamp is not implemented.
- Optimization lifecycle records are in-memory objects. Durable audit storage is
  still needed for long-term compliance and weekly report generation.
- The manifest editor is intentionally narrow and supports the current Kustomize
  `/spec/replicas` patch shape. Different manifest layouts need explicit tests.
- GitHub PR creation requires `GREENOPS_GITOPS_CREATE_PULL_REQUEST=true`,
  `GREENOPS_GITOPS_GITHUB_REPOSITORY`, and `GREENOPS_GITOPS_GITHUB_TOKEN`.
- Carbon savings in weekly reports are estimates derived from measured replica
  and carbon data plus configured power assumptions.
