"""Build each setup's manifests as Flux applies them, settings substituted, for make lint-kube-linter.

kube-linter decodes every object into Kubernetes's own types and silently skips one it cannot decode. A resource
limit of "${GPU_I915}" is not a quantity until Flux substitutes it, so linting the files as they are would leave
Jellyfin's and Frigate's Deployments out of every check without a word. So, as Flux does, this builds each path a
setup's Flux Kustomizations apply (k8s/clusters/<setup>) with kustomize, then substitutes the setup's placeholder
settings (tests/fixtures/cluster-settings.yaml) in every object that does not opt out, and writes the result to
<out>/<setup>/<Kustomization>.yaml for kube-linter. SOPS-encrypted Secrets are left out. A Flux Kustomization that
patches what it builds is refused, since its patches would be missing here.

A manifest under k8s/ that no setup applies fails the run, so a file cannot drop out of the checks unnoticed.

kustomize comes from PATH.

    python scripts/ci/render_manifests.py --out .cache/k8s-rendered
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from render_helm import ROOT, files_of, objects_in, substituted, synced_files

K8S = ROOT / "k8s"
SUBSTITUTE = "kustomize.toolkit.fluxcd.io/substitute"


def flux_paths(setup):
    """The paths the setup's Flux Kustomizations build from this repository, by Kustomization name."""
    out = {}
    for path in files_of(K8S / "clusters" / setup):
        for obj in objects_in(path):
            # A Kustomization applying another repository's files (Intel's GPU plugin) has nothing here to build.
            if obj["kind"] == "Kustomization" and obj["spec"]["sourceRef"]["name"] == "flux-system":
                name = obj["metadata"]["name"]
                if obj["spec"].get("patches") or obj["spec"].get("images"):
                    sys.exit(f"{setup}: {name} patches what it builds, which this does not reproduce")
                out[name] = ROOT / obj["spec"]["path"]
    return out


def as_applied(obj, settings):
    """An object with the settings substituted, unless it opts out."""
    if ((obj.get("metadata") or {}).get("annotations") or {}).get(SUBSTITUTE) == "disabled":
        return obj
    return substituted(obj, settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    kustomize = shutil.which("kustomize") or sys.exit("kustomize is not on PATH (brew install kustomize)")
    fixtures = yaml.safe_load((ROOT / "tests" / "fixtures" / "cluster-settings.yaml").read_text())
    shutil.rmtree(args.out, ignore_errors=True)
    applied = set()
    for setup in sorted(p.name for p in (K8S / "clusters").iterdir() if p.is_dir()):
        applied |= set(synced_files(setup))
        (args.out / setup).mkdir(parents=True)
        for name, path in flux_paths(setup).items():
            command = [kustomize, "build", "--load-restrictor=LoadRestrictionsNone", str(path)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                sys.exit(f"{setup}: {name}: {result.stderr.strip()}")
            objects = [
                as_applied(obj, fixtures[setup])
                for obj in yaml.safe_load_all(result.stdout)
                if obj and not (obj["kind"] == "Secret" and "sops" in obj)
            ]
            (args.out / setup / f"{name}.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))
    manifests = {
        path.resolve()
        for path in K8S.rglob("*.y*ml")
        if path.name != "kustomization.yaml" and not path.name.endswith((".sops.yaml", ".sops.yml"))
    }
    unapplied = sorted(str(path.relative_to(ROOT)) for path in manifests - applied if objects_in(path))
    if unapplied:
        sys.exit("No setup applies these, so nothing checks them: " + ", ".join(unapplied))
    print(f"{len(fixtures)} setups -> {args.out}")


if __name__ == "__main__":
    main()
