# argocd

Argo CD GitOps deployment integration for GreenOps AI.

---

## Deployment Path

The AI Agent **never** modifies Kubernetes directly.
Every infrastructure change travels the following path:

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                    GreenOps Deployment Pipeline                      │
 └─────────────────────────────────────────────────────────────────────┘

 1. GreenOps AI Agent
    │  Reads carbon intensity (Electricity Maps → Prometheus)
    │  Reads workload state (Prometheus → kube-state-metrics)
    │  Produces: DecisionRecommendation (action, replicas, reason)
    │
    ▼
 2. OptimizationSafetyPolicy  (agent/safety.py — Phase 7)
    │  Hard rejections: missing signals, stale carbon data, SLA breaches
    │  Review conditions: cooldown, large scale-downs
    │  Produces: PolicyValidation (APPROVED / REJECTED / REQUIRE_REVIEW)
    │
    ▼  [only if APPROVED and approved_for_gitops_change=true]
    │
 3. GitOpsChangeWorkflow  (gitops/workflow.py — Phase 8)
    │  Creates a dedicated greenops/... branch
    │  Patches ONLY k8s/overlays/<env>/kustomization.yaml /spec/replicas
    │  Commits with full AI decision + policy validation audit trail
    │  Opens a GitHub Pull Request for human review
    │  ⚠ Never touches Kubernetes, Argo CD, or any production API
    │
    ▼  [human reviews + merges the PR]
    │
 4. GitHub (source of truth)
    │  main branch contains the approved desired state
    │
    ▼
 5. Argo CD (this directory)
    │  Detects diff between Git desired state and cluster actual state
    │  Syncs k8s/overlays/<env>/ → Kubernetes cluster
    │  Health checks confirm rollout success
    │
    ▼
 6. Kubernetes
    │  Deployment controller reconciles to the new replica count
    │  Prometheus scrapes updated metrics
    │
    ▼  [next agent poll cycle]
    └─ GreenOps AI Agent observes the new replica count via Prometheus
```

---

## A Replica Change — Step by Step

This traces a single carbon-driven scale-down from AI observation to running pod:

| Step | Actor | Action | Output |
|---|---|---|---|
| 1 | Prometheus | Scrapes `greenops_carbon_intensity_gco2_per_kwh = 312` | Time-series stored |
| 2 | AI Agent | Queries Prometheus; observes high carbon, low CPU | `DecisionRecommendation(action=SCALE_DOWN, replicas=1, reason="...")` |
| 3 | Safety Policy | Checks: availability ≥ 1.0, P99 < 1s, no errors, cooldown elapsed | `PolicyValidation(status=APPROVED, approved_for_gitops_change=true)` |
| 4 | GitOps Workflow | Creates branch `greenops/scale-down-greenops-demo-workload-to-1-high-carbon` | `k8s/overlays/prod/kustomization.yaml`: `value: 1` (the agent's default `GREENOPS_GITOPS_MANIFEST_PATH`) |
| 5 | GitOps Workflow | Commits with AI + policy metadata embedded | Commit SHA recorded |
| 6 | GitOps Workflow | Opens GitHub PR | PR title: "GreenOps: Scale Down greenops-demo-workload to 1 replicas" |
| 7 | Human reviewer | Reviews carbon context, approves, merges PR | `main` branch updated |
| 8 | Human operator | `argocd app sync greenops-workload-prod` (no auto-sync) | Argo CD: OutOfSync → Syncing → Healthy |
| 9 | Kubernetes | Deployment controller (namespace `greenops`) reduces replicas 2 → 1 | Old pod terminated gracefully |
| 10 | Prometheus | Next scrape: `kube_deployment_spec_replicas{namespace="greenops"} = 1` | Carbon × scale correlation visible in Grafana |

> To drive the **dev** overlay instead, set `GREENOPS_GITOPS_MANIFEST_PATH=k8s/overlays/dev/kustomization.yaml`
> and `K8S_NAMESPACE=greenops-dev`, and sync `greenops-workload-dev`.

---

## Directory Layout

```
argocd/
├── project.yaml                          # AppProject — scopes source repo + destination namespace
├── app-of-apps.yaml                      # Root Application — manages all platform config as code
├── repo-secret.template.yaml             # Credential template — replace value before applying
├── install/
│   └── kustomization.yaml                # Installs the Argo CD control plane (core only)
└── applications/
    ├── greenops-workload-dev.yaml         # Dev environment Application
    └── greenops-workload-prod.yaml        # Production environment Application
```

---

## Bootstrap — First-Time Setup

```bash
# 1. Connect kubectl to your cluster
kubectl cluster-info

# 2. Create the repository credential secret (NEVER commit the real token)
kubectl create secret generic greenops-repo-secret \
  -n argocd \
  --from-literal=type=git \
  --from-literal=url=https://github.com/kumardeepak9/carbon-aware-cloud-optimizer.git \
  --from-literal=username=git \
  --from-literal=password=<your-read-only-github-token>

# 3. Label the secret so Argo CD picks it up
kubectl label secret greenops-repo-secret \
  -n argocd \
  argocd.argoproj.io/secret-type=repository

# 4. Install the Argo CD control plane
kubectl apply -k argocd/install/

# 5. Wait for Argo CD to start
kubectl -n argocd rollout status deployment/argocd-server

# 6. Bootstrap the GreenOps platform config (AppProject + workload Applications)
#    Kept separate from step 4: kustomize will not load a file outside the
#    kustomization directory, and this is the standard Argo CD bootstrap.
kubectl apply -f argocd/app-of-apps.yaml

# 7. Get the initial admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d && echo

# 8. Port-forward Argo CD UI
kubectl port-forward svc/argocd-server -n argocd 8080:443
# → https://localhost:8080  (admin / <password above>)
```

---

## Day-to-Day Operations

```bash
# Check application sync status
argocd app list

# Manually sync dev after a PR merge
argocd app sync greenops-workload-dev --prune

# Rollback to the previous revision
argocd app rollback greenops-workload-dev

# Watch sync in real time
argocd app wait greenops-workload-dev --health

# Trigger a hard refresh (re-reads Git, bypasses cache)
argocd app get greenops-workload-dev --hard-refresh
```

---

## Sync Policy Design

| Environment | Auto-Sync | Prune | Self-Heal | Rationale |
|---|---|---|---|---|
| `dev` | Disabled (default) | — | — | Change requires human PR review + explicit sync |
| `prod` | Disabled | — | — | Production changes always require human trigger |

> **Why no auto-sync?**
> Auto-sync would allow a merged PR to immediately modify Kubernetes without a human
> explicitly initiating the sync. For production, this bypasses the last human gate.
> Enable `automated.selfHeal: true` only after the team is comfortable with the full pipeline.

---

## Security Notes

| Concern | Mitigation |
|---|---|
| Argo CD credential scope | Read-only fine-grained PAT; separate from GitOps workflow write token |
| Secret storage | `repo-secret.template.yaml` contains only a placeholder — real secrets via Sealed Secrets or ESO |
| Namespace isolation | AppProject allows only `greenops` (prod) and `greenops-dev` (dev); the two overlays never share live objects |
| Cluster-scope access | Only `Namespace` resource allowed at cluster level |
| AI → Kubernetes | Impossible by design — agent has no Kubernetes API credentials |
| Credential rotation | Rotate the Argo CD repo PAT independently of the GitOps workflow PAT |
