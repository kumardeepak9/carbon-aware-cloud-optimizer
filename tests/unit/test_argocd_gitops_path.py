"""
Static validation of the Argo CD configuration and the GitHub -> Argo CD ->
Kubernetes reconciliation path.

No cluster is required. These tests parse the checked-in manifests and assert
the properties the deployment flow depends on:

  * one repository, consistent across every Application, the AppProject and the
    repo-credential template
  * each workload Application targets a namespace the AppProject actually allows
  * dev and prod never manage the same live objects
  * workload sync is human-triggered (no automated sync)
  * the app-of-apps manages only the project + workload Applications
  * `kubectl apply -k argocd/install/` is loadable (no parent-dir escape)
  * a replica change committed to Git renders into the manifest Argo CD applies
  * no Kubernetes API write path exists in the code or the dependency set
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ARGOCD = REPO / "argocd"
REPO_URL = "https://github.com/kumardeepak9/carbon-aware-cloud-optimizer.git"

WORKLOAD_APPS = [
    ARGOCD / "applications" / "greenops-workload-dev.yaml",
    ARGOCD / "applications" / "greenops-workload-prod.yaml",
]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _kustomize(path: Path) -> list[dict]:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl not available for kustomize rendering")
    out = subprocess.run(
        [kubectl, "kustomize", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


# ---------------------------------------------------------------------------
# Repository / revision consistency
# ---------------------------------------------------------------------------


def test_all_sources_point_at_one_repository() -> None:
    urls: set[str] = set()
    for app in [ARGOCD / "app-of-apps.yaml", *WORKLOAD_APPS]:
        urls.add(_load(app)["spec"]["source"]["repoURL"])
    urls.update(_load(ARGOCD / "project.yaml")["spec"]["sourceRepos"])
    urls.add(_load(ARGOCD / "repo-secret.template.yaml")["stringData"]["url"])
    assert urls == {REPO_URL}


def test_repo_url_matches_git_origin() -> None:
    try:
        origin = subprocess.run(
            ["git", "-C", str(REPO), "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("no git origin")
    assert origin == REPO_URL


def test_every_application_tracks_main() -> None:
    for app in [ARGOCD / "app-of-apps.yaml", *WORKLOAD_APPS]:
        assert _load(app)["spec"]["source"]["targetRevision"] == "main"


# ---------------------------------------------------------------------------
# AppProject scoping
# ---------------------------------------------------------------------------


def test_workload_applications_use_the_greenops_project() -> None:
    project = _load(ARGOCD / "project.yaml")["metadata"]["name"]
    for app in WORKLOAD_APPS:
        assert _load(app)["spec"]["project"] == project


def test_application_destinations_are_permitted_by_the_project() -> None:
    proj = _load(ARGOCD / "project.yaml")["spec"]
    allowed = {(d["server"], d["namespace"]) for d in proj["destinations"]}
    for app in WORKLOAD_APPS:
        dest = _load(app)["spec"]["destination"]
        assert (dest["server"], dest["namespace"]) in allowed

    # The project must not silently allow extra namespaces.
    assert allowed == {
        ("https://kubernetes.default.svc", "greenops"),
        ("https://kubernetes.default.svc", "greenops-dev"),
    }


def test_project_only_grants_namespace_at_cluster_scope() -> None:
    proj = _load(ARGOCD / "project.yaml")["spec"]
    assert proj["clusterResourceWhitelist"] == [{"group": "", "kind": "Namespace"}]


# ---------------------------------------------------------------------------
# dev / prod isolation
# ---------------------------------------------------------------------------


def test_dev_and_prod_target_different_namespaces() -> None:
    ns = {app.stem: _load(app)["spec"]["destination"]["namespace"] for app in WORKLOAD_APPS}
    assert ns["greenops-workload-dev"] != ns["greenops-workload-prod"]


def test_overlay_namespace_matches_its_application_destination() -> None:
    for app, overlay in [
        (WORKLOAD_APPS[0], REPO / "k8s/overlays/dev"),
        (WORKLOAD_APPS[1], REPO / "k8s/overlays/prod"),
    ]:
        app_ns = _load(app)["spec"]["destination"]["namespace"]
        rendered = _kustomize(overlay)
        rendered_ns = {
            d["metadata"].get("namespace")
            for d in rendered
            if d["kind"] != "Namespace"
        }
        assert rendered_ns == {app_ns}, f"{overlay}: {rendered_ns} != {app_ns}"
        ns_objs = [d["metadata"]["name"] for d in rendered if d["kind"] == "Namespace"]
        assert ns_objs == [app_ns]


def test_dev_and_prod_never_manage_the_same_object() -> None:
    def ids(overlay: str) -> set[tuple[str, str, str]]:
        return {
            (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace", ""))
            for d in _kustomize(REPO / "k8s/overlays" / overlay)
        }

    assert ids("dev").isdisjoint(ids("prod"))


# ---------------------------------------------------------------------------
# Sync policy — reconciliation is human-triggered for workloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app", WORKLOAD_APPS, ids=lambda p: p.stem)
def test_workload_sync_is_not_automated(app: Path) -> None:
    sync_policy = _load(app)["spec"].get("syncPolicy", {})
    assert "automated" not in sync_policy, (
        f"{app.stem}: automated sync would let a merged PR change the cluster "
        "with no human trigger"
    )


@pytest.mark.parametrize("app", WORKLOAD_APPS, ids=lambda p: p.stem)
def test_workload_prune_is_conservative(app: Path) -> None:
    opts = _load(app)["spec"]["syncPolicy"]["syncOptions"]
    assert "PrunePropagationPolicy=foreground" in opts
    assert "PruneLast=true" in opts


# ---------------------------------------------------------------------------
# app-of-apps scope + bootstrap loadability
# ---------------------------------------------------------------------------


def test_app_of_apps_manages_only_project_and_workload_apps() -> None:
    directory = _load(ARGOCD / "app-of-apps.yaml")["spec"]["source"]["directory"]
    assert directory["recurse"] is True
    include = directory.get("include", "")
    # It must scope by include (not a bare recurse) and must not pull in the
    # install kustomization or the secret template.
    assert "project.yaml" in include and "applications/*.yaml" in include
    assert "exclude" not in directory


def test_app_of_apps_is_not_self_managed() -> None:
    include = _load(ARGOCD / "app-of-apps.yaml")["spec"]["source"]["directory"]["include"]
    assert "app-of-apps" not in include


def test_install_kustomization_has_no_parent_directory_escape() -> None:
    kust = _load(ARGOCD / "install" / "kustomization.yaml")
    for entry in kust.get("resources", []):
        assert not str(entry).startswith(".."), (
            "kustomize refuses to load files above the kustomization root; "
            "`kubectl apply -k argocd/install/` would fail"
        )


def test_install_kustomization_builds() -> None:
    rendered = _kustomize(ARGOCD / "install")
    kinds = {d["kind"] for d in rendered}
    assert "CustomResourceDefinition" in kinds
    assert "Deployment" in kinds
    # It installs Argo CD core only — no GreenOps Application in the bundle.
    assert not any(d.get("apiVersion", "").startswith("argoproj.io/") for d in rendered)


# ---------------------------------------------------------------------------
# GitHub desired state -> (Argo CD would apply) -> Kubernetes
# ---------------------------------------------------------------------------


def test_controlled_replica_change_reaches_the_rendered_manifest(tmp_path: Path) -> None:
    from gitops.manifest import update_kustomize_replica_patch

    work = tmp_path / "k8s"
    shutil.copytree(REPO / "k8s", work)
    overlay = work / "overlays" / "prod"
    kfile = overlay / "kustomization.yaml"

    before = [d for d in _kustomize(overlay) if d["kind"] == "Deployment"][0]
    start = before["spec"]["replicas"]
    target = start - 1

    patched = update_kustomize_replica_patch(
        kfile.read_text(), deployment_name="greenops-demo-workload", replicas=target
    )
    assert patched.changed and patched.previous_replicas == start
    kfile.write_text(patched.content)

    after = [d for d in _kustomize(overlay) if d["kind"] == "Deployment"][0]
    assert after["spec"]["replicas"] == target
    # Only the replica count changed — image, resources, probes are untouched.
    assert after["spec"]["template"] == before["spec"]["template"]


# ---------------------------------------------------------------------------
# No AI Agent -> Kubernetes write path
# ---------------------------------------------------------------------------


def test_no_kubernetes_client_dependency() -> None:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    names = {d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in deps}
    assert "kubernetes" not in names
    assert not any("kube" in n for n in names)


def test_no_kubernetes_client_import_in_source() -> None:
    offenders: list[str] = []
    for pkg in ("agent", "gitops", "monitoring", "carbon", "config", "reports"):
        for py in (REPO / pkg).rglob("*.py"):
            text = py.read_text()
            for needle in ("import kubernetes", "from kubernetes", "kubernetes.client"):
                if needle in text:
                    offenders.append(f"{py.relative_to(REPO)}: {needle}")
    assert offenders == []


def test_kubernetes_settings_carries_no_credentials() -> None:
    from config.settings import KubernetesSettings

    fields = set(KubernetesSettings.model_fields)
    assert fields == {"namespace"}, f"unexpected k8s settings: {fields}"
