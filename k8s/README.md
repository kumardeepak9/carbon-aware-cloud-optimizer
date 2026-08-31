# k8s — Kubernetes Workload Manifests

All Kubernetes resources for the Carbon-Aware Cloud Optimizer, structured as a **Kustomize base + overlays**.

## Layout

```
k8s/
├── base/                        # Environment-agnostic resources
│   ├── namespace.yaml           # namespace (renamed per overlay)
│   ├── configmap.yaml           # Non-secret runtime config
│   ├── deployment.yaml          # Demo workload Deployment
│   ├── service.yaml             # ClusterIP Service
│   └── kustomization.yaml
└── overlays/
    ├── dev/                     # Dev: 1 replica, debug logging — namespace greenops-dev
    │   └── kustomization.yaml
    └── prod/                    # Prod: 3 replicas, full resources — namespace greenops
        └── kustomization.yaml
```

> dev and prod deploy to **different namespaces** (`greenops-dev` / `greenops`) so
> their Argo CD Applications never contend for the same objects on one cluster.
> The monitoring stack and the agent's default config target `greenops` (prod).

## Apply

```bash
# Preview what will be applied (dry run)
kubectl diff -k k8s/overlays/dev

# Apply dev environment
kubectl apply -k k8s/overlays/dev

# Apply prod environment
kubectl apply -k k8s/overlays/prod
```

## GreenOps AI — Replica Control

Phase 6's GreenOps AI agent is **read-only**. It consumes Prometheus and grid
signals and returns a recommendation only; it does not commit to Git, change
Kubernetes resources, or interact with Argo CD. Applying a recommendation is
reserved for a future explicitly approved control phase.

| Carbon Intensity | Agent Action | Replica Target |
|---|---|---|
| `< 100 gCO2eq/kWh` (low) | Scale up | 5–6 replicas |
| `100–250 gCO2eq/kWh` (medium) | Hold | 3 replicas |
| `> 250 gCO2eq/kWh` (high) | Scale down | 1–2 replicas |

## Security Highlights

- Runs as **non-root** (uid 1001)
- **Read-only root filesystem** (writable `/tmp` via emptyDir)
- All Linux **capabilities dropped**
- `allowPrivilegeEscalation: false`
- `seccompProfile: RuntimeDefault`
