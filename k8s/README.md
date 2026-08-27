# k8s — Kubernetes Workload Manifests

All Kubernetes resources for the Carbon-Aware Cloud Optimizer, structured as a **Kustomize base + overlays**.

## Layout

```
k8s/
├── base/                        # Environment-agnostic resources
│   ├── namespace.yaml           # greenops namespace
│   ├── configmap.yaml           # Non-secret runtime config
│   ├── deployment.yaml          # Demo workload Deployment
│   ├── service.yaml             # ClusterIP Service
│   └── kustomization.yaml
└── overlays/
    ├── dev/                     # Dev: 1 replica, debug logging, local image
    │   └── kustomization.yaml
    └── prod/                    # Prod: 3 replicas, full resources, GHCR image
        └── kustomization.yaml
```

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

The GreenOps AI agent scales the `greenops-demo-workload` Deployment by **committing a change to `/spec/replicas`** in `k8s/overlays/prod/kustomization.yaml` and pushing via GitOps.

Argo CD detects the commit and reconciles the cluster state.

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
