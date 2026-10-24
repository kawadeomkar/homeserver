"""Render every chart a setup's Flux Kustomizations install, for just lint charts.

For each setup (k8s/clusters/<setup>), follows its Flux Kustomizations' paths through their kustomization.yaml files,
as kustomize does, to the HelmReleases there and their sources (an OCIRepository's URL, tag and digest, a
GitRepository's URL and commit, or a HelmRepository's URL). Each release's values get the setup's placeholder settings
(tests/fixtures/cluster-settings.yaml) where they name a ${VAR}, as Flux would substitute them, and `helm template`
renders the chart at its pin into <out>/<setup>/<namespace>-<release>.yaml: an OCI chart by tag and digest, which helm
refuses once the tag names another digest, and a Git one from its commit, fetched alone. A chart with a values schema
(Traefik's) then refuses a misspelt key.

helm and git come from PATH, with helm's caches and the Git checkouts under .cache/helm, so nothing is read from or
written to the user's own.

    python scripts/ci/render_helm.py --out .cache/helm-rendered
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# Kinds that have no namespace, among those charts render.
CLUSTER_SCOPED = {
    "APIService",
    "ClusterRole",
    "ClusterRoleBinding",
    "CSIDriver",
    "CustomResourceDefinition",
    "IngressClass",
    "MutatingWebhookConfiguration",
    "Namespace",
    "PersistentVolume",
    "PriorityClass",
    "StorageClass",
    "ValidatingWebhookConfiguration",
}
SUBSTITUTION = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?=([^}]*))?\}")


def objects_in(path):
    """The objects in a manifest file; a kustomization.yaml holds none."""
    documents = yaml.safe_load_all(path.read_text())
    return [
        d
        for d in documents
        if isinstance(d, dict)
        and d.get("kind")
        and str(d.get("apiVersion", "")).split("/")[0] != "kustomize.config.k8s.io"
    ]


def files_of(directory):
    """The files a kustomize build of `directory` reads, following its resources."""
    kustomization = yaml.safe_load((directory / "kustomization.yaml").read_text())
    found = []
    for resource in kustomization.get("resources") or []:
        target = (directory / resource).resolve()
        found += files_of(target) if target.is_dir() else [target]
    return found


def synced_files(setup):
    """Every file the setup's Flux Kustomizations apply, its own files first."""
    out = files_of(ROOT / "k8s" / "clusters" / setup)
    for path in list(out):
        for obj in objects_in(path):
            # Only a Kustomization of this repository (source flux-system) has files here.
            if obj["kind"] == "Kustomization" and obj["spec"]["sourceRef"]["name"] == "flux-system":
                out += [file for file in files_of(ROOT / obj["spec"]["path"]) if file not in out]
    return out


def synced_objects(setup):
    """Every object the setup's Flux Kustomizations apply."""
    return [obj for path in synced_files(setup) for obj in objects_in(path)]


def substituted(value, settings):
    """A value with ${VAR} and ${VAR:=default} replaced, as Flux's postBuild substitution does."""
    if isinstance(value, str):

        def one(match):
            if match.group(1) in settings:
                return str(settings[match.group(1)])
            if match.group(2) is not None:
                return match.group(2)
            sys.exit(f"{match.group(0)} is not a placeholder setting in tests/fixtures/cluster-settings.yaml")

        return SUBSTITUTION.sub(one, value).replace("$${", "${")
    if isinstance(value, dict):
        return {k: substituted(v, settings) for k, v in value.items()}
    if isinstance(value, list):
        return [substituted(v, settings) for v in value]
    return value


def git_chart(source, path, cache):
    """A chart from a GitRepository pinned to a commit: that commit's chart directory, fetched alone (one commit,
    shallow, and only the blobs under the chart's path), as Flux would check it out."""
    commit = source["spec"]["ref"].get("commit")
    if not commit:
        sys.exit(f"GitRepository {source['metadata']['name']} names no commit, which this does not reproduce")
    checkout = cache / "git" / commit
    chart = checkout / path.removeprefix("./")
    if not chart.is_dir():
        shutil.rmtree(checkout, ignore_errors=True)
        checkout.mkdir(parents=True)
        for command in (
            ["init", "-q"],
            ["fetch", "-q", "--depth", "1", "--filter=blob:none", source["spec"]["url"], commit],
            ["sparse-checkout", "set", "--no-cone", "/" + path.removeprefix("./").strip("/") + "/"],
            ["checkout", "-q", "FETCH_HEAD"],
        ):
            subprocess.run(["git", "-C", str(checkout), *command], check=True)
    return chart


def chart_args(release, sources, cache):
    """helm template's chart arguments for a HelmRelease: the chart, its repository and its version. An OCI chart
    pinned by digest is rendered by tag and digest, which helm refuses when the tag no longer names that digest."""
    spec = release["spec"]
    namespace = release["metadata"]["namespace"]
    if "chartRef" in spec:
        ref = spec["chartRef"]
        source = sources[(ref["kind"], ref.get("namespace", namespace), ref["name"])]
        pin = source["spec"]["ref"]
        if pin.get("digest"):
            return [f"{source['spec']['url']}:{pin['tag']}@{pin['digest']}"]
        return [source["spec"]["url"], "--version", pin["tag"]]
    chart = spec["chart"]["spec"]
    ref = chart["sourceRef"]
    source = sources[(ref["kind"], ref.get("namespace", namespace), ref["name"])]
    if ref["kind"] == "GitRepository":
        return [str(git_chart(source, chart["chart"], cache))]
    return [chart["chart"], "--repo", source["spec"]["url"], "--version", str(chart["version"])]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--kube-version", default="1.37.1")
    args = parser.parse_args()
    helm = shutil.which("helm") or sys.exit("helm is not on PATH (brew install helm)")
    cache = ROOT / ".cache" / "helm"
    env = {
        **os.environ,
        "HELM_CACHE_HOME": str(cache / "cache"),
        "HELM_CONFIG_HOME": str(cache / "config"),
        "HELM_DATA_HOME": str(cache / "data"),
    }
    fixtures = yaml.safe_load((ROOT / "tests" / "fixtures" / "cluster-settings.yaml").read_text())
    shutil.rmtree(args.out, ignore_errors=True)
    failed = False
    for setup in sorted(p.name for p in (ROOT / "k8s" / "clusters").iterdir() if p.is_dir()):
        objects = synced_objects(setup)
        sources = {
            (o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]): o
            for o in objects
            if o["kind"] in ("HelmRepository", "OCIRepository", "GitRepository")
        }
        (args.out / setup).mkdir(parents=True, exist_ok=True)
        for release in (o for o in objects if o["kind"] == "HelmRelease"):
            name = release["metadata"]["name"]
            # What reaches the cluster would differ from what is rendered here, and the checks would read the wrong
            # thing: refused until this reproduces them.
            unsupported = sorted({"postRenderers", "valuesFrom", "valuesFiles"} & set(release["spec"]))
            unsupported += (
                ["chart.spec.valuesFiles"]
                if "valuesFiles" in ((release["spec"].get("chart") or {}).get("spec") or {})
                else []
            )
            if unsupported:
                sys.exit(f"{setup}: {name} sets {', '.join(unsupported)}, which this does not reproduce")
            namespace = release["spec"].get("targetNamespace") or release["metadata"]["namespace"]
            values = substituted(release["spec"].get("values") or {}, fixtures[setup])
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
                yaml.safe_dump(values, file)
                file.flush()
                command = [helm, "template", name, *chart_args(release, sources, cache), "--namespace", namespace]
                # Without the charts' test pods, which Flux never runs.
                command += ["--kube-version", args.kube_version, "--values", file.name, "--skip-tests"]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
            if result.returncode:
                print(f"{setup}: {namespace}/{name}: {result.stderr.strip()}", file=sys.stderr)
                failed = True
                continue
            # helm install puts an object without a namespace in the release's; the checks need to see where it runs.
            documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
            for doc in documents:
                if doc.get("kind") not in CLUSTER_SCOPED:
                    doc.setdefault("metadata", {}).setdefault("namespace", namespace)
            target = args.out / setup / f"{namespace}-{name}.yaml"
            target.write_text(yaml.safe_dump_all(documents, sort_keys=False))
            print(f"{setup}: {namespace}/{name} -> {target}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
