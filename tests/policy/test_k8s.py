"""The Kubernetes manifests keep the conventions CLAUDE.md sets for them that kube-linter cannot check.

Flux syncs k8s/clusters/<setup>/, whose Flux Kustomizations each apply one path of k8s/; a path's files are found by
following its kustomization.yaml's resources, as kustomize does. kustomization.yaml files themselves, and documents
without a kind, are not objects.

- Every Flux Kustomization decrypts SOPS secrets and substitutes the cluster's settings, and every ${VAR} a synced
  file names without a default is one of those settings in each setup that syncs it. A ConfigMap that holds a foreign
  template wholesale opts out with the substitute: disabled annotation.
- Each namespace is declared once, and every namespaced object names a declared namespace or one the cluster brings
  (kube-system, flux-system). A controller's namespace is declared in the Flux Kustomization that applies the
  controller, or in one it depends on, so its Pod Security label arrives with it.
- No Service opens a port on the nodes: no NodePort, and every LoadBalancer sets allocateLoadBalancerNodePorts: false.
- The namespaces labelled for privileged pods are exactly the ones listed below, each label says why in a comment,
  and each namespace still holds a pod that needs it, or a chart's or a controller's pods do.
- Only those namespaces and kube-system hold pods that Pod Security Admission's baseline level rejects for the
  privilege they ask for: privileged containers, added capabilities, host namespaces and host ports.
- The hostPath volumes, in pods and in PersistentVolumes, are exactly the ones listed below; in what the charts
  render (just lint charts), the ones listed for them.
- The apps (k8s/apps/) run one replica each, replaced whole (Recreate), Homepage aside; every container refuses
  privilege escalation under the RuntimeDefault seccomp profile; every Ingress uses Traefik and the domain setting;
  every pod that mounts a shared NAS or static volume chowns its mount point first, and none sets fsGroup on an NFS
  volume, which would chown the whole volume at every start.
- No PodDisruptionBudget: with one node for the apps, it would only stop a drain.
- Every app namespace has a NetworkPolicy over all its pods that limits who reaches them and keeps them from the one
  hosts they must never reach (EGRESS_DENIED_ADDRESS, the NAS or the Proxmox host without one, and PROXMOX_ADDRESS).
- No service account outside kube-system and flux-system may read Secrets in every namespace, flux-system's age key
  among them, bar the few listed below; in what the charts render too.
- Every Secret is SOPS-encrypted.
- LinuxServer.io images use the immutable x.y.z-lsNNN form of their tags.
- Every container's requests and limits, and every volume's size, is a Kubernetes quantity once Flux substitutes the
  settings: kubeconform accepts any string there, and kube-linter silently skips an object it cannot decode.

kube-linter (.kube-linter.yaml) covers the rest: privileged containers and added capabilities without an
annotation saying why, unpinned images, imagePullPolicy Always, and missing CPU and memory requests.
"""

import functools
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from helpers import CLUSTER_ARGS, PROXMOX_ONLY_CLUSTER_LOCAL_YML, as_local_yml, example, example_tree, resolve_on

ROOT = Path(__file__).resolve().parents[2]
# The apps' manifests. The tests that must find an app skip until there is one, rather than pass on nothing.
APPS = ROOT / "k8s" / "apps"
K8S = ROOT / "k8s"
CLUSTERS = K8S / "clusters"
# What just lint charts rendered the charts into (its HELM_RENDERED), .cache/helm-rendered by default.
RENDERED = ROOT / os.environ.get("HELM_RENDERED", ".cache/helm-rendered")

# Pod Security Admission exempts these, so they need no label. Talos exempts kube-system by default.
SYSTEM_NAMESPACES = {"kube-system"}
# Namespaces an object may name without the repo declaring them: the cluster or the Flux Operator makes them.
# flux-system is not exempt from Pod Security, so it stays out of SYSTEM_NAMESPACES.
UNDECLARED_NAMESPACES = {"kube-system", "flux-system"}

# The namespaces labelled pod-security.kubernetes.io/enforce: privileged. Changing the set needs a reason in
# the pull request, and the label a comment saying why.
PRIVILEGED_NAMESPACES = {"intel-gpu-plugin", "local-path-storage", "metallb-system", "monitoring"}
# Of those, the ones whose privileged pods only a chart renders (MetalLB's speaker, node-exporter): checked against
# what just lint charts renders. The others' need shows in the repository's own manifests (Intel's GPU plugin,
# vendored; the Job that makes the static volumes' directories in local-path-storage).
CHART_PRIVILEGED = {"metallb-system", "monitoring"}

# Every hostPath volume, as (object, path): the one Job that makes the local static volumes' directories without a
# NAS. Pod Security Admission's baseline level rejects one in a pod spec, but does not look inside a PersistentVolume,
# and the local volumes are `local`, not hostPath. Adding one needs a reason in the pull request.
HOST_PATHS = {
    ("Job local-path-storage/static-volume-directories", "/var/mnt"),
    # Intel's GPU plugin, vendored (k8s/infrastructure/gpu): the device, its sysfs class, the kubelet's device-plugin
    # sockets and the CDI specifications it writes, in the privileged namespace intel-gpu-plugin.
    ("DaemonSet intel-gpu-plugin/intel-gpu-plugin", "/dev/dri"),
    ("DaemonSet intel-gpu-plugin/intel-gpu-plugin", "/sys/class/drm"),
    ("DaemonSet intel-gpu-plugin/intel-gpu-plugin", "/var/lib/kubelet/device-plugins"),
    ("DaemonSet intel-gpu-plugin/intel-gpu-plugin", "/var/run/cdi"),
}

# The hostPath volumes in what the charts render (just lint charts): a chart bump that adds one fails here before
# it merges. csi-driver-nfs mounts the kubelet's directories, in kube-system, which Talos exempts from Pod Security;
# node-exporter reads the host's root, /proc and /sys, in monitoring; MetalLB's speaker needs none.
CHART_HOST_PATHS = {
    ("DaemonSet kube-system/csi-nfs-node", "/var/lib/kubelet/plugins/csi-nfsplugin"),
    ("DaemonSet kube-system/csi-nfs-node", "/var/lib/kubelet/plugins_registry"),
    ("DaemonSet kube-system/csi-nfs-node", "/var/lib/kubelet/pods"),
    ("Deployment kube-system/csi-nfs-controller", "/var/lib/kubelet/pods"),
    ("DaemonSet monitoring/kube-prometheus-stack-prometheus-node-exporter", "/"),
    ("DaemonSet monitoring/kube-prometheus-stack-prometheus-node-exporter", "/proc"),
    ("DaemonSet monitoring/kube-prometheus-stack-prometheus-node-exporter", "/sys"),
}

# The SELinux types baseline allows (Kubernetes Pod Security Standards); none set is fine too.
BASELINE_SELINUX_TYPES = {"", "container_t", "container_init_t", "container_kvm_t", "container_engine_t"}

# The capabilities baseline lets a container add (Kubernetes Pod Security Standards).
BASELINE_CAPABILITIES = {
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_CHROOT",
}

CLUSTER_SCOPED = {
    "ClusterIssuer",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "IngressClass",
    "Namespace",
    "PersistentVolume",
    "PriorityClass",
    "StorageClass",
}

# Where each workload kind keeps its pod spec.
POD_SPEC = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}

LINUXSERVER = re.compile(r"^(?:(?:lscr\.io|ghcr\.io|docker\.io)/)?linuxserver/")
IMMUTABLE = re.compile(r":[^:@/]+-ls\d+$|@sha256:[0-9a-f]{64}$")
PRIVILEGED_LABEL = re.compile(r"""^\s*pod-security\.kubernetes\.io/enforce:\s*["']?privileged["']?\s*$""")
# A Flux substitution: ${NAME}, or ${NAME:=default}. $${NAME} is a literal ${NAME}, never substituted.
SUBSTITUTION = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(:?=[^}]*)?\}")
SUBSTITUTE_DISABLED = "kustomize.toolkit.fluxcd.io/substitute"
# What Kubernetes parses as a quantity: a number, then a binary or decimal suffix, or an exponent.
QUANTITY = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([KMGTPE]i|[numkMGTPE]|[eE][+-]?\d+)?$")


def objects_in(path):
    """The objects in one manifest file: kustomization.yaml files and documents without a kind are none."""
    return [
        obj
        for obj in yaml.safe_load_all(path.read_text())
        if isinstance(obj, dict)
        and obj.get("kind")
        and str(obj.get("apiVersion", "")).split("/")[0] != "kustomize.config.k8s.io"
    ]


@functools.cache
def manifests():
    """Every object under k8s/, as (path relative to the repository, object). Comment-only files hold none."""
    found = []
    for path in sorted(K8S.rglob("*.y*ml")):
        found.extend((path.relative_to(ROOT), obj) for obj in objects_in(path))
    assert len(found) > 40, "the manifests were not found"
    return tuple(found)


def of_kind(kind):
    return [(path, obj) for path, obj in manifests() if obj["kind"] == kind]


def namespace(obj):
    return obj["metadata"].get("namespace")


def label(obj):
    """How a failure names an object: its kind, then namespace/name, or only its name if it is cluster-scoped."""
    kind, name = obj["kind"], obj["metadata"]["name"]
    return f"{kind} {name}" if kind in CLUSTER_SCOPED else f"{kind} {namespace(obj)}/{name}"


def pod_spec(obj):
    """A workload's pod spec, or None for any other kind."""
    if obj["kind"] not in POD_SPEC:
        return None
    spec = obj
    for key in POD_SPEC[obj["kind"]]:
        spec = spec[key]
    return spec


def workloads(objects=None):
    return [(path, obj, pod_spec(obj)) for path, obj in (objects or manifests()) if obj["kind"] in POD_SPEC]


def containers(spec):
    return [
        container
        for key in ("initContainers", "containers", "ephemeralContainers")
        for container in spec.get(key) or []
    ]


def beyond_baseline(spec):
    """The privilege a pod spec asks for that Pod Security Admission's baseline level rejects (hostPath aside)."""
    found = [field for field in ("hostNetwork", "hostPID", "hostIPC") if spec.get(field)]
    if ((spec.get("securityContext") or {}).get("seccompProfile") or {}).get("type") == "Unconfined":
        found.append("seccomp Unconfined")
    if ((spec.get("securityContext") or {}).get("seLinuxOptions") or {}).get("type", "") not in BASELINE_SELINUX_TYPES:
        found.append("an SELinux type of its own")
    for container in containers(spec):
        context = container.get("securityContext") or {}
        name = container["name"]
        if context.get("privileged"):
            found.append(f"{name}: privileged")
        if (context.get("seLinuxOptions") or {}).get("type", "") not in BASELINE_SELINUX_TYPES:
            found.append(f"{name}: SELinux type {context['seLinuxOptions']['type']}")
        added = set((context.get("capabilities") or {}).get("add") or []) - BASELINE_CAPABILITIES
        if added:
            found.append(f"{name}: adds {', '.join(sorted(added))}")
        if any(port.get("hostPort") for port in container.get("ports") or []):
            found.append(f"{name}: hostPort")
        if (context.get("seccompProfile") or {}).get("type") == "Unconfined":
            found.append(f"{name}: seccomp Unconfined")
    return found


def host_paths(obj):
    """The hostPath volumes in an object, as (object, path)."""
    if obj["kind"] == "PersistentVolume":
        volumes = [obj["spec"]]
    elif obj["kind"] in POD_SPEC:
        volumes = pod_spec(obj).get("volumes") or []
    else:
        return set()
    return {(label(obj), volume["hostPath"]["path"]) for volume in volumes if "hostPath" in volume}


# ------------------------------------------------------------------------------------------ Flux's model ---


@functools.cache
def files_of(directory):
    """The files a kustomize build of `directory` (relative to the repository) reads, following its resources."""
    base = ROOT / directory
    kustomization = yaml.safe_load((base / "kustomization.yaml").read_text())
    found = {(base / "kustomization.yaml").relative_to(ROOT)}
    for resource in kustomization.get("resources") or []:
        target = (base / resource).resolve()
        if target.is_dir():
            found |= files_of(str(target.relative_to(ROOT)))
        else:
            found.add(target.relative_to(ROOT))
    return frozenset(found)


@functools.cache
def flux_kustomizations():
    """Every Flux Kustomization each setup syncs, as {setup: {name: (path, dependsOn names)}}."""
    out = {}
    for cluster in sorted(p for p in CLUSTERS.iterdir() if p.is_dir()):
        out[cluster.name] = {}
        for path in files_of(str(cluster.relative_to(ROOT))):
            for obj in objects_in(ROOT / path):
                # Only one of this repository (source flux-system) has anything here to follow.
                if obj["kind"] == "Kustomization" and obj["spec"]["sourceRef"]["name"] == "flux-system":
                    spec = obj["spec"]
                    depends = tuple(d["name"] for d in spec.get("dependsOn") or [])
                    out[cluster.name][obj["metadata"]["name"]] = (spec["path"].removeprefix("./"), depends)
    return out


def synced_files(setup):
    """Every file a setup's Flux Kustomizations apply."""
    return frozenset().union(*(files_of(path) for path, _ in flux_kustomizations()[setup].values()))


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def substitutions(obj):
    """The ${NAME} an object asks Flux for, without a default; none when it opts out of substitution."""
    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    if annotations.get(SUBSTITUTE_DISABLED) == "disabled":
        return set()
    return {m.group(1) for s in strings_in(obj) for m in SUBSTITUTION.finditer(s) if not m.group(2)}


# ---------------------------------------------------------------------------------------------- tests ---


def node_port_services(objects):
    """The Services that open a port on every node: a NodePort, or a LoadBalancer that allocates them."""
    wrong = []
    for _, service in objects:
        if service["kind"] != "Service":
            continue
        spec = service["spec"]
        kind = spec.get("type", "ClusterIP")
        if kind == "NodePort" or (kind == "LoadBalancer" and spec.get("allocateLoadBalancerNodePorts", True)):
            wrong.append(label(service))
    return wrong


def test_no_service_opens_a_node_port():
    """Every app is reached through Traefik's ingress or a LoadBalancer address; a node port would bypass both."""
    wrong = node_port_services(manifests())
    assert wrong == [], "use ClusterIP and an Ingress, or a LoadBalancer with allocateLoadBalancerNodePorts: false"


def test_k8s_holds_only_yaml():
    """kustomize reads JSON as readily as YAML, but nothing here does: the leak rules, these tests and the renders read
    YAML alone, so a manifest written as JSON would pass them all. Configuration templates are .tmpl, notes .md."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "k8s"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    wrong = sorted(name for name in listed if name and Path(name).suffix not in (".yaml", ".yml", ".tmpl", ".md"))
    assert wrong == [], "write manifests as YAML"


PRUNE = "kustomize.toolkit.fluxcd.io/prune"


def test_what_holds_data_is_never_pruned():
    """Every claim, every volume and every namespace holding a claim carries prune: disabled, so data outlives its
    manifest: a claim renamed or removed is never deleted by Flux, and its data orphaned."""
    claimed = {namespace(obj) for _, obj in of_kind("PersistentVolumeClaim")}
    wrong = [
        label(obj)
        for path, obj in manifests()
        if (
            obj["kind"] in ("PersistentVolumeClaim", "PersistentVolume")
            or (obj["kind"] == "Namespace" and obj["metadata"]["name"] in claimed)
        )
        and ((obj["metadata"].get("annotations") or {}).get(PRUNE)) != "disabled"
    ]
    assert wrong == [], f"annotate it {PRUNE}: disabled (CLAUDE.md, Storage model)"


# What Flux's substitution makes of a form the scripts and tests do not emulate: $$ becomes $, and a shell form such as
# ${VAR:-x} or ${VAR#x} is substituted or, with strict substitution, fails the whole build.
UNEMULATED = re.compile(r"\$\$(?!\{)|(?<!\$)\$\{(?![A-Za-z_][A-Za-z0-9_]*(?::?=[^}]*)?\})")


def test_only_substitutions_the_checks_reproduce():
    """The renders and these tests substitute ${VAR} and ${VAR:=default}, and keep $${VAR} as a literal ${VAR}. Flux
    also rewrites $$ and the shell forms, which they would not: a script's kill $$ would reach the cluster as kill $."""
    wrong = []
    for setup in flux_kustomizations():
        for path in sorted(synced_files(setup)):
            for obj in objects_in(ROOT / path):
                if ((obj.get("metadata") or {}).get("annotations") or {}).get(SUBSTITUTE_DISABLED) == "disabled":
                    continue
                wrong += [f"{path}: {label(obj)}: {s!r}" for s in strings_in(obj) if UNEMULATED.search(s)]
    assert sorted(set(wrong)) == [], "write a literal $ as $${...} or avoid $$, and ${VAR} or ${VAR:=default} only"


def test_each_namespace_is_declared_once():
    declared = defaultdict(list)
    for path, obj in of_kind("Namespace"):
        declared[obj["metadata"]["name"]].append(str(path))
    assert {name: paths for name, paths in declared.items() if len(paths) > 1} == {}, (
        "a duplicate Namespace fails a kustomize build, and under Flux pruning one owner deletes the namespace"
    )


def test_namespaced_objects_name_a_declared_namespace():
    declared = {obj["metadata"]["name"] for _, obj in of_kind("Namespace")} | UNDECLARED_NAMESPACES
    wrong = [
        f"{path}: {label(obj)}"
        for path, obj in manifests()
        if obj["kind"] not in CLUSTER_SCOPED and namespace(obj) not in declared
    ]
    assert wrong == [], "name a namespace declared in this repo (or kube-system, flux-system), so none is guessed"


def test_controllers_bring_their_namespaces():
    """A Namespace declared after the controller that runs in it, or never, leaves the controller's pods rejected by
    Pod Security, the Kustomization never ready, and everything that depends on it never applied."""
    where = {obj["metadata"]["name"]: path for path, obj in of_kind("Namespace")}
    wrong = []
    for setup, kustomizations in flux_kustomizations().items():
        for name, (path, depends) in kustomizations.items():
            reachable = files_of(path).union(*(files_of(kustomizations[d][0]) for d in depends))
            for file in files_of(path):
                for obj in objects_in(ROOT / file):
                    if obj["kind"] in POD_SPEC:
                        target = namespace(obj)
                    elif obj["kind"] == "HelmRelease":
                        target = obj["spec"].get("targetNamespace") or namespace(obj)
                    else:
                        continue
                    if target not in UNDECLARED_NAMESPACES and where.get(target) not in reachable:
                        wrong.append(f"{setup}/{name}: {label(obj)} runs in {target}, declared in {where.get(target)}")
    assert wrong == [], "declare a controller's Namespace beside it, in its own Flux Kustomization"


def test_every_flux_kustomization_decrypts_and_substitutes():
    wrong = []
    for setup in flux_kustomizations():
        for file in files_of(str((CLUSTERS / setup).relative_to(ROOT))):
            for obj in objects_in(ROOT / file):
                if obj["kind"] != "Kustomization":
                    continue
                spec = obj["spec"]
                if (spec.get("decryption") or {}).get("secretRef", {}).get("name") != "sops-age":
                    wrong.append(f"{setup}/{obj['metadata']['name']}: no decryption with sops-age")
                sources = (spec.get("postBuild") or {}).get("substituteFrom") or []
                if {"kind": "ConfigMap", "name": "cluster-settings"} not in sources:
                    wrong.append(f"{setup}/{obj['metadata']['name']}: no substitution from cluster-settings")
    assert wrong == [], "a SOPS Secret or a ${VAR} added later must not stall a Kustomization (CLAUDE.md)"


# The Flux Kustomizations whose path differs by setup: the controllers (csi-driver-nfs with a NAS) and the static
# volumes (NFS shares with a NAS, directories on the worker without one).
SETUP_PATHS = {"infra-controllers", "infra-storage"}


def test_both_setups_sync_the_same_kustomizations():
    """k8s/clusters/nas and k8s/clusters/local apply the same Flux Kustomizations with the same dependencies, and from
    the same paths but for the two whose path ends in the setup's name: a change made to one setup alone fails here."""
    nas, local = flux_kustomizations()["nas"], flux_kustomizations()["local"]
    assert sorted(nas) == sorted(local)
    wrong = []
    for name, (path, depends) in nas.items():
        other_path, other_depends = local[name]
        same = other_path == path.removesuffix("/nas") + "/local" if name in SETUP_PATHS else other_path == path
        if not same or depends != other_depends:
            wrong.append(f"{name}: {path} {depends} with a NAS, {other_path} {other_depends} without")
    assert wrong == []


def static_volumes(setup):
    """The PersistentVolumes a setup's infra-storage applies, as {name: what the apps' claims rely on}."""
    out = {}
    for path in files_of(flux_kustomizations()[setup]["infra-storage"][0]):
        for obj in objects_in(ROOT / path):
            if obj["kind"] == "PersistentVolume":
                spec = obj["spec"]
                claim = (spec["claimRef"]["namespace"], spec["claimRef"]["name"])
                out[obj["metadata"]["name"]] = {
                    "capacity": spec["capacity"]["storage"],
                    "accessModes": sorted(spec["accessModes"]),
                    "storageClassName": spec.get("storageClassName"),
                    "reclaim": spec.get("persistentVolumeReclaimPolicy"),
                    "claim": claim,
                    "prune": obj["metadata"].get("annotations", {}).get(PRUNE),
                }
    return out


def test_both_setups_define_the_same_static_volumes():
    """The shared volumes have the same names, sizes, access modes and claims in both setups, so the same app
    manifests bind in either: only their source differs (an NFS share, or a directory on the worker)."""
    nas, local = static_volumes("nas"), static_volumes("local")
    assert nas and nas == local


def size(quantity):
    """A storage quantity in bytes: the binary suffixes the manifests use, or none."""
    for suffix, factor in (("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("Ti", 2**40)):
        if quantity.endswith(suffix):
            return float(quantity.removesuffix(suffix)) * factor
    return float(quantity)


def test_static_volumes_bind_the_apps_claims():
    """Each static volume's claim exists, names the volume, and asks for no more than it holds and the same access."""
    claims = {(namespace(obj), obj["metadata"]["name"]): obj for _, obj in of_kind("PersistentVolumeClaim")}
    volumes = static_volumes("nas")
    if not any(v["claim"] in claims for v in volumes.values()):
        pytest.skip("no app claims a static volume yet")
    wrong = []
    for name, volume in volumes.items():
        claim = claims.get(volume["claim"])
        spec = claim["spec"] if claim else {}
        if (
            not claim
            or spec.get("volumeName") != name
            or sorted(spec.get("accessModes", [])) != volume["accessModes"]
            or spec.get("storageClassName") != volume["storageClassName"]
            or size(spec["resources"]["requests"]["storage"]) > size(volume["capacity"])
        ):
            wrong.append(f"{name}: claim {volume['claim']}")
    assert wrong == []


def test_nas_volumes_mount_the_datasets_the_nas_exports(tmp_path):
    """With a NAS, the static volumes mount exactly the datasets kubernetes_nas_datasets has the NAS create and
    export to the worker: the volumes name their paths themselves, so a dataset renamed in one place alone fails
    here. Pool names renamed in local.yml reach both."""
    local_yml = as_local_yml(example() | {"truenas_pool_persistent": "tank", "truenas_pool_ephemeral": "scratch"})
    datasets, root, values = resolve_on(
        example_tree(tmp_path, local_yml),
        "homeserver-k8s",
        "[kubernetes_nas_datasets | map(attribute='name') | list, vm_storage_mount_root, flux_bootstrap_settings]",
        *CLUSTER_ARGS,
    )
    shares = set()
    for path in files_of(flux_kustomizations()["nas"]["infra-storage"][0]):
        for obj in yaml.safe_load_all(substitute((ROOT / path).read_text(), values)):
            if isinstance(obj, dict) and obj.get("kind") == "PersistentVolume":
                shares.add(obj["spec"]["csi"]["volumeAttributes"]["share"])
    assert shares == {f"{root}/{name}" for name in datasets}, (shares, datasets)
    assert all(name.split("/")[0] in ("tank", "scratch") for name in datasets), datasets


def test_substituted_files_are_synced():
    """A ${VAR} in a file no Flux Kustomization applies would reach the cluster as text, if it ever got there."""
    synced = frozenset().union(*(synced_files(setup) for setup in flux_kustomizations()))
    wrong = sorted({str(path) for path, obj in manifests() if substitutions(obj) and path not in synced})
    assert wrong == []


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    """The cluster-settings keys each setup's local.yml gives, resolved as roles/flux_bootstrap resolves them."""
    out = {}
    for setup, local_yml in (("nas", None), ("local", PROXMOX_ONLY_CLUSTER_LOCAL_YML)):
        tree = example_tree(tmp_path_factory.mktemp(setup), local_yml)
        out[setup] = set(resolve_on(tree, "homeserver-k8s", "flux_bootstrap_settings", *CLUSTER_ARGS))
    return out


def test_substitutions_are_settings(settings):
    """Flux's strict substitution fails a whole Kustomization over a ${VAR} that is not a setting: found here first."""
    wrong = []
    for setup in flux_kustomizations():
        synced = synced_files(setup)
        for path, obj in manifests():
            if path in synced:
                wrong += [
                    f"{setup}: {path}: {label(obj)}: ${{{name}}}" for name in substitutions(obj) - settings[setup]
                ]
    assert wrong == [], "add the key to flux_bootstrap_settings (group_vars/kubernetes/flux.yml), or give it a default"


def substitute(text, settings):
    """Text with each ${NAME} replaced by its setting, or by its default, as Flux does."""
    return SUBSTITUTION.sub(lambda m: str(settings.get(m.group(1), (m.group(2) or "").partition("=")[2])), text)


def quantities(obj):
    """An object's quantities, as (where, value): its containers' requests and limits, a claim's request, a
    volume's capacity."""
    if obj["kind"] == "PersistentVolumeClaim":
        return [("storage", value) for value in ((obj["spec"].get("resources") or {}).get("requests") or {}).values()]
    if obj["kind"] == "PersistentVolume":
        return [("capacity", value) for value in (obj["spec"].get("capacity") or {}).values()]
    if obj["kind"] not in POD_SPEC:
        return []
    return [
        (f"{container['name']} {kind} {resource}", value)
        for container in containers(pod_spec(obj))
        for kind, amounts in (container.get("resources") or {}).items()
        if kind in ("requests", "limits")
        for resource, value in (amounts or {}).items()
    ]


def test_quantities_parse_once_substituted():
    """A resource limit of "${GPU_I915}" becomes a quantity only when Flux substitutes it, and a typo such as 2Gb never
    does. kubeconform accepts any string there, and kube-linter silently skips an object it cannot decode, taking it
    out of every check: just lint manifests lints the substituted manifests, and this makes sure they parse."""
    fixtures = yaml.safe_load((ROOT / "tests" / "fixtures" / "cluster-settings.yaml").read_text())
    wrong = []
    for setup in flux_kustomizations():
        for path in sorted(synced_files(setup)):
            for obj in objects_in(ROOT / path):
                for where, value in quantities(obj):
                    text = substitute(str(value), fixtures[setup])
                    if not QUANTITY.match(text):
                        wrong.append(f"{setup}: {path}: {label(obj)}: {where}: {value!r} -> {text!r}")
    assert wrong == [], "not a Kubernetes quantity"


def test_privileged_namespaces_are_pinned_explained_and_needed():
    labelled = {
        obj["metadata"]["name"]: path
        for path, obj in of_kind("Namespace")
        if (obj["metadata"].get("labels") or {}).get("pod-security.kubernetes.io/enforce") == "privileged"
    }
    assert set(labelled) == PRIVILEGED_NAMESPACES
    unexplained = []
    for path in sorted(set(labelled.values())):
        lines = (ROOT / path).read_text().splitlines()
        for number, line in enumerate(lines):
            if PRIVILEGED_LABEL.match(line) and not lines[number - 1].lstrip().startswith("#"):
                unexplained.append(f"{path}:{number + 1}")
    assert unexplained == [], "say why in a comment on the line above the label"
    needed = {namespace(obj) for _, obj, spec in workloads() if beyond_baseline(spec) or host_paths(obj)}
    assert PRIVILEGED_NAMESPACES - needed - CHART_PRIVILEGED == set(), "nothing in these needs the label any more"


def test_privileged_pods_only_in_privileged_namespaces():
    allowed = PRIVILEGED_NAMESPACES | SYSTEM_NAMESPACES
    wrong = {
        label(obj): found
        for _, obj, spec in workloads()
        if (found := beyond_baseline(spec)) and namespace(obj) not in allowed
    }
    assert wrong == {}, "Talos enforces baseline: label the namespace and say why (CLAUDE.md, adding a service)"


def test_local_volumes_stay_under_var_mnt():
    """A local PersistentVolume gives its pods the node's directory as a hostPath would, but Pod Security never looks
    at it: each stays under /var/mnt, the Talos user volumes, once the settings are substituted."""
    fixtures = yaml.safe_load((ROOT / "tests" / "fixtures" / "cluster-settings.yaml").read_text())
    wrong = []
    for setup in flux_kustomizations():
        for path in sorted(synced_files(setup)):
            for obj in objects_in(ROOT / path):
                local = (obj.get("spec") or {}).get("local") if obj["kind"] == "PersistentVolume" else None
                where = substitute(str(local["path"]), fixtures[setup]) if local else "/var/mnt/"
                if not where.startswith("/var/mnt/") or ".." in where.split("/"):
                    wrong.append(f"{setup}: {label(obj)}: {where}")
    assert wrong == [], "keep local volumes on the node's data or media volume, under /var/mnt"


def test_host_paths_are_pinned():
    found = set().union(*(host_paths(obj) for _, obj in manifests()))
    assert sorted(found - HOST_PATHS) == [], "use a PersistentVolumeClaim, not a new hostPath (CLAUDE.md)"
    assert sorted(HOST_PATHS - found) == [], "these are gone: remove them from the list"


def test_no_pod_disruption_budget():
    assert of_kind("PodDisruptionBudget") == [], "with one node for the apps, a PodDisruptionBudget only blocks a drain"


def test_secrets_are_sops_encrypted():
    plaintext = [f"{path}: {label(obj)}" for path, obj in of_kind("Secret") if "sops" not in obj]
    assert plaintext == [], "encrypt it with SOPS (k8s/README-secrets.md)"


def test_linuxserver_tags_are_immutable():
    if not APPS.is_dir():
        pytest.skip("no apps under k8s/apps/ yet")
    images = {
        container["image"]: label(obj)
        for _, obj, spec in workloads()
        for container in containers(spec)
        if LINUXSERVER.match(container["image"])
    }
    assert images, "no LinuxServer.io image found"
    mutable = {image: owner for image, owner in images.items() if not IMMUTABLE.search(image)}
    assert mutable == {}, "LinuxServer.io re-points x.y.z at every build: pin x.y.z-lsNNN (CLAUDE.md)"


# --------------------------------------------------------------------------------- what the charts render ---


@functools.cache
def rendered():
    """What just lint charts rendered, as (path, object); empty when it has not run (just test policy alone,
    offline)."""
    if not RENDERED.is_dir():
        return ()
    found = []
    for path in sorted(RENDERED.rglob("*.yaml")):
        found.extend(
            (path.relative_to(ROOT), obj) for obj in yaml.safe_load_all(path.read_text()) if obj and obj.get("kind")
        )
    return tuple(found)


DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# An image named in a flag, such as cert-manager's ACME solver or the Prometheus operator's config reloader.
IMAGE_FLAG = re.compile(r"^--[a-z0-9-]+=((?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s=]+)$")


def image_refs(objects):
    """(owner, image) for every image the objects run: their containers', those named in a container's flags, and a
    Prometheus's or an Alertmanager's own."""
    for path, obj in objects:
        spec = pod_spec(obj)
        for container in containers(spec) if spec else []:
            yield path, label(obj), container["image"]
            for arg in (container.get("args") or []) + (container.get("command") or []):
                if match := IMAGE_FLAG.match(str(arg)):
                    yield path, label(obj), match.group(1)
        if obj["kind"] in ("Prometheus", "Alertmanager") and obj["spec"].get("image"):
            yield path, label(obj), obj["spec"]["image"]


def kustomize_digests(path):
    """The images a file's kustomization.yaml pins by digest, by the name they replace."""
    kustomization = ROOT / path.parent / "kustomization.yaml"
    if not kustomization.is_file():
        return {}
    return {
        entry["name"]: entry["digest"]
        for entry in yaml.safe_load(kustomization.read_text()).get("images") or []
        if entry.get("digest")
    }


def test_images_are_pinned_by_digest():
    """A tag can be moved to other contents and a digest cannot, so every image is pinned by both: the tag says what
    it is, the digest what is pulled. A vendored file keeps upstream's tag, and its kustomization.yaml the digest."""
    wrong = []
    for path, owner, image in image_refs(manifests()):
        if not DIGEST.search(image) and image.rpartition(":")[0] not in kustomize_digests(path):
            wrong.append(f"{owner}: {image}")
    assert wrong == []


def test_chart_sources_are_pinned():
    """Every chart is pulled by something its publisher cannot move: an OCI artifact's digest, or a Git commit. A
    Helm repository's index can point a version at other contents at any time, so none is used."""
    wrong = [f"{label(obj)}: no digest" for _, obj in of_kind("OCIRepository") if not obj["spec"]["ref"].get("digest")]
    wrong += [f"{label(obj)}: no commit" for _, obj in of_kind("GitRepository") if not obj["spec"]["ref"].get("commit")]
    wrong += [f"{label(obj)}: a Helm repository" for _, obj in of_kind("HelmRepository")]
    assert wrong == []


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_chart_images_are_pinned_by_digest():
    """Every image a chart runs is pinned in the release's values: the chart's defaults are tags."""
    wrong = sorted({f"{owner}: {image}" for _, owner, image in image_refs(rendered()) if not DIGEST.search(image)})
    assert wrong == []


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_chart_host_paths_are_pinned():
    found = set().union(*(host_paths(obj) for _, obj in rendered()))
    assert sorted(found - CHART_HOST_PATHS) == [], "a chart mounts a new hostPath: check why, then pin it here"
    assert sorted(CHART_HOST_PATHS - found) == [], "these are gone from the charts: remove them from the list"


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_charts_open_no_node_port():
    """Traefik's LoadBalancer comes from its chart: a chart bump that starts allocating node ports fails here."""
    assert node_port_services(rendered()) == [], "set the chart's value that keeps node ports off"


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_chart_privileged_namespaces_are_still_needed():
    """A namespace labelled privileged for a chart's sake still holds a rendered pod that needs it: a chart bump that
    drops node-exporter's host namespaces must not leave monitoring privileged for good."""
    needed = {namespace(obj) for _, obj, spec in workloads(rendered()) if beyond_baseline(spec) or host_paths(obj)}
    assert sorted(CHART_PRIVILEGED - needed) == [], (
        "no chart's pod needs the label any more: drop it, and from the list"
    )


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_chart_privileged_pods_only_in_privileged_namespaces():
    allowed = PRIVILEGED_NAMESPACES | SYSTEM_NAMESPACES
    wrong = {
        f"{path}: {label(obj)}": found
        for path, obj, spec in workloads(rendered())
        if (found := beyond_baseline(spec)) and namespace(obj) not in allowed
    }
    assert wrong == {}, "a chart's pod needs more than baseline: label its namespace, saying why"


@pytest.mark.skipif(not RENDERED.is_dir(), reason="just lint charts renders the charts first")
def test_chart_scrapes_send_no_token_over_plain_http():
    """kube-prometheus-stack gives its scrapes of the cluster's components Prometheus's service account token, the
    plain-HTTP ones too (etcd, kube-proxy, CoreDNS), where it would cross the LAN or the pod network in clear. None of
    those asks for one, so the release's values take it off them."""
    wrong = [
        f"{label(obj)}: {endpoint.get('port') or endpoint.get('targetPort')}"
        for _, obj in rendered()
        if obj["kind"] in ("ServiceMonitor", "PodMonitor")
        for endpoint in obj["spec"].get("endpoints") or obj["spec"].get("podMetricsEndpoints") or []
        if endpoint.get("scheme", "http") != "https"
        and any(endpoint.get(key) for key in ("authorization", "bearerTokenFile", "bearerTokenSecret", "basicAuth"))
    ]
    assert wrong == [], "set the chart's serviceMonitor.authorization to null for these"


def test_chart_fixtures_match_the_settings(settings):
    """just lint charts renders the charts with tests/fixtures/cluster-settings.yaml's placeholders; each setup
    there has exactly the keys roles/flux_bootstrap puts in that setup's cluster-settings."""
    fixtures = yaml.safe_load((ROOT / "tests" / "fixtures" / "cluster-settings.yaml").read_text())
    assert {setup: set(keys) for setup, keys in fixtures.items()} == settings


# The service accounts outside kube-system and flux-system that may read every Secret in the cluster, as
# namespace/name, each with its reason. Any of them reaches flux-system's age key, which decrypts every SOPS file in
# this public repository's history, so one more needs a reason in the pull request. kube-system's (csi-driver-nfs's
# provisioner) and flux-system's (Flux, cluster-admin by design) are not listed.
CLUSTER_SECRET_READERS = {
    # Reads the TLS Secrets its routes name. Its chart grants every Secret, and namespaced RBAC, the chart's one way to
    # narrow that, cannot serve the IngressClass. It answers the LAN: the one to narrow next.
    "traefik/traefik",
    # Writes Prometheus's and Alertmanager's configuration as Secrets and reads those a ServiceMonitor names. It
    # answers nothing.
    "monitoring/kube-prometheus-stack-operator",
    # cert-manager writes each certificate into a Secret in its Certificate's namespace, and its cainjector copies CA
    # data out of Secrets into webhook configurations. Neither answers the LAN; their chart has no namespaced RBAC.
    "cert-manager/cert-manager",
    "cert-manager/cert-manager-cainjector",
}
# Kubernetes's own ClusterRoles that read Secrets, which a binding can name without the repository defining them.
BUILTIN_SECRET_ROLES = {"cluster-admin", "admin", "edit"}


def reads_every_secret(rule):
    """Whether an RBAC rule lets its holder read Secrets it does not name: resourceNames narrows a rule to them."""
    return (
        {"", "*"} & set(rule.get("apiGroups") or [])
        and {"secrets", "*"} & set(rule.get("resources") or [])
        and {"get", "list", "watch", "*"} & set(rule.get("verbs") or [])
        and not rule.get("resourceNames")
    )


def cluster_secret_readers(objects):
    """The service accounts a ClusterRoleBinding lets read Secrets in every namespace, as namespace/name. A
    RoleBinding to a ClusterRole grants it in that namespace alone, so it is not one."""
    roles = {
        obj["metadata"]["name"]
        for _, obj in objects
        if obj["kind"] == "ClusterRole" and any(reads_every_secret(rule) for rule in obj.get("rules") or [])
    } | BUILTIN_SECRET_ROLES
    return {
        f"{subject['namespace']}/{subject['name']}"
        for _, obj in objects
        if obj["kind"] == "ClusterRoleBinding" and obj["roleRef"]["name"] in roles
        for subject in obj.get("subjects") or []
        if subject["kind"] == "ServiceAccount"
    }


def test_few_read_every_secret():
    """Grafana's chart once gave its LAN-facing pod every Secret, and kube-state-metrics lists them all for its
    metrics: both are narrowed in their HelmRelease's values, and a chart bump that widens one fails here."""
    readers = cluster_secret_readers(manifests() + rendered())
    wrong = sorted(r for r in readers if r.split("/")[0] not in {"kube-system", "flux-system"})
    assert sorted(set(wrong) - CLUSTER_SECRET_READERS) == [], (
        "these can read every Secret, flux-system's age key included: narrow their role, or list them with a reason"
    )
    if rendered():
        assert sorted(CLUSTER_SECRET_READERS - set(wrong)) == [], (
            "these read every Secret no more: drop them from the list"
        )


def test_secret_readers_are_found():
    """The check above finds a binding to a role that reads Secrets, and lets through a rule narrowed by name."""
    role = {
        "kind": "ClusterRole",
        "metadata": {"name": "r"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}],
    }
    named = {
        "kind": "ClusterRole",
        "metadata": {"name": "n"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"], "resourceNames": ["one"]}],
    }

    def binding(name, ref):
        return {
            "kind": "ClusterRoleBinding",
            "metadata": {"name": name},
            "roleRef": {"name": ref},
            "subjects": [{"kind": "ServiceAccount", "namespace": "apps", "name": name}],
        }

    objects = [(None, o) for o in (role, named, binding("a", "r"), binding("b", "n"), binding("c", "edit"))]
    assert cluster_secret_readers(objects) == {"apps/a", "apps/c"}


# ------------------------------------------------------------------------------------------- the apps ---

# The static volumes several apps or a fresh dataset share, whose mount point an init container makes uid 1000.
SHARED_VOLUMES = {"media", "nextcloud-data", "photoprism-media"}


def app_workloads():
    return [(path, obj, spec) for path, obj, spec in workloads() if path.is_relative_to(APPS.relative_to(ROOT))]


def claims_of(spec):
    """The claims a pod spec mounts, by volume name."""
    return {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in spec.get("volumes") or []
        if "persistentVolumeClaim" in v
    }


def bound_volumes():
    """{(namespace, claim): volume} for every claim bound to a static volume by name."""
    return {
        (namespace(obj), obj["metadata"]["name"]): obj["spec"]["volumeName"]
        for _, obj in of_kind("PersistentVolumeClaim")
        if obj["spec"].get("volumeName")
    }


def nfs_volumes():
    return {obj["metadata"]["name"] for _, obj in of_kind("PersistentVolume") if "csi" in obj["spec"]}


def test_apps_run_one_replica_replaced_whole():
    """Their volumes hold one app's files at a time; Homepage, which holds nothing, rolls two."""
    wrong = []
    for _, obj, _ in app_workloads():
        if obj["kind"] != "Deployment":
            wrong.append(f"{label(obj)}: a {obj['kind']}, not a Deployment")
            continue
        replicas, strategy = obj["spec"].get("replicas"), (obj["spec"].get("strategy") or {}).get("type")
        expected = (2, "RollingUpdate") if obj["metadata"]["name"] == "homepage" else (1, "Recreate")
        if (replicas, strategy) != expected:
            wrong.append(f"{label(obj)}: {replicas} replicas, {strategy}")
    assert wrong == []


# Vendored whole from upstream, and changed only by kustomize patches, which a raw read does not see.
VENDORED = {Path("k8s/infrastructure/controllers/common/local-path/local-path-storage.yaml")}


def test_containers_are_hardened():
    wrong = []
    for _, obj, spec in (w for w in workloads() if w[0] not in VENDORED):
        pod_seccomp = ((spec.get("securityContext") or {}).get("seccompProfile") or {}).get("type")
        for container in containers(spec):
            context = container.get("securityContext") or {}
            if context.get("allowPrivilegeEscalation") is not False:
                wrong.append(f"{label(obj)}/{container['name']}: allowPrivilegeEscalation is not false")
            if (context.get("seccompProfile") or {}).get("type", pod_seccomp) != "RuntimeDefault":
                wrong.append(f"{label(obj)}/{container['name']}: no RuntimeDefault seccomp profile")
    assert wrong == []


def test_app_containers_cannot_forge_packets():
    """No app container keeps NET_RAW: raw sockets would let one forge packets to its neighbours on the node's
    bridge and read the plain HTTP between Traefik and them. One that needs no capability drops them all."""
    wrong = []
    for _, obj, spec in app_workloads():
        for container in containers(spec):
            dropped = ((container.get("securityContext") or {}).get("capabilities") or {}).get("drop") or []
            if not {"ALL", "NET_RAW"} & set(dropped):
                wrong.append(f"{label(obj)}/{container['name']}: keeps NET_RAW")
    assert wrong == []


def test_app_pods_hold_no_token():
    """No app reads the Kubernetes API, so none gets a service account token a compromise could use."""
    wrong = [label(obj) for _, obj, spec in app_workloads() if spec.get("automountServiceAccountToken") is not False]
    assert wrong == []


def test_ingresses_use_traefik_and_the_domain():
    wrong = []
    for _, obj in of_kind("Ingress"):
        spec = obj["spec"]
        if spec.get("ingressClassName") != "traefik":
            wrong.append(f"{label(obj)}: ingressClassName is not traefik")
        for rule in spec.get("rules") or []:
            if not rule.get("host", "").endswith("${DOMAIN}"):
                wrong.append(f"{label(obj)}: host {rule.get('host')} is not <app>.${{DOMAIN}}")
        if any(a.split("/")[0] == "nginx.ingress.kubernetes.io" for a in obj["metadata"].get("annotations") or {}):
            wrong.append(f"{label(obj)}: an nginx annotation, which Traefik ignores")
    assert wrong == []


def test_shared_volumes_are_chowned_first():
    """A new dataset's mount point belongs to root; the apps run as 1000."""
    bound = bound_volumes()
    wrong = []
    for _, obj, spec in app_workloads():
        for volume, claim in claims_of(spec).items():
            if bound.get((namespace(obj), claim)) not in SHARED_VOLUMES:
                continue
            chown = [c for c in spec.get("initContainers") or [] if c["name"] == "chown-mount"]
            mounts = [m for c in chown for m in c.get("volumeMounts") or [] if m["name"] == volume]
            if not mounts or mounts[0]["mountPath"] != "/mnt/v":
                wrong.append(f"{label(obj)}: mounts {claim} without chown-mount on it")
    assert wrong == []


def test_no_fs_group_on_nfs_volumes():
    """kubelet would chown and chmod a whole NFS volume, the library or the photos, at every start of the pod."""
    bound, nfs = bound_volumes(), nfs_volumes()
    wrong = []
    for _, obj, spec in workloads():
        context = spec.get("securityContext") or {}
        if "fsGroup" not in context or context.get("fsGroupChangePolicy") == "OnRootMismatch":
            continue
        claims = [c for c in claims_of(spec).values() if bound.get((namespace(obj), c)) in nfs]
        if claims:
            wrong.append(f"{label(obj)}: fsGroup with {', '.join(claims)}")
    assert wrong == []


def test_apps_are_labelled_by_name():
    wrong = [
        label(obj)
        for path, obj in manifests()
        if path.is_relative_to(APPS.relative_to(ROOT))
        and obj["kind"] not in ("Namespace", "PersistentVolumeClaim", "ConfigMap")
        and "app.kubernetes.io/name" not in (obj["metadata"].get("labels") or {})
    ]
    assert wrong == []


# The hosts no app pod may reach: the NAS (the Proxmox host without one), and the Proxmox host.
DENIED_EGRESS = ("${EGRESS_DENIED_ADDRESS}/32", "${PROXMOX_ADDRESS}/32")


def test_app_namespaces_are_fenced():
    """Each app namespace has a NetworkPolicy over every pod in it, for traffic in and out: in, its ingress rules name
    who may connect (an app namespace never takes the whole cluster); out, any address wide enough to cover the NAS
    and the Proxmox host leaves both out: the NAS's NFS exports trust the node's address, which pods' traffic leaves
    with, and the host's API and SSH answer the LAN."""
    if not APPS.is_dir():
        pytest.skip("no apps under k8s/apps/ yet")
    app_namespaces = {obj["metadata"]["name"] for path, obj in of_kind("Namespace") if path.is_relative_to("k8s/apps")}
    assert app_namespaces, "no app namespace found"
    fenced, wrong = set(), []
    for _, obj in of_kind("NetworkPolicy"):
        spec = obj["spec"]
        if namespace(obj) not in app_namespaces:
            continue
        if spec.get("podSelector") == {} and {"Ingress", "Egress"} <= set(spec.get("policyTypes") or []):
            fenced.add(namespace(obj))
        for rule in spec.get("ingress") or []:
            if not rule.get("from"):
                wrong.append(f"{label(obj)}: an ingress rule without from admits every pod and address")
        for rule in spec.get("egress") or []:
            for peer in rule.get("to") or [{}]:
                block = peer.get("ipBlock")
                if peer == {} or (
                    block and not set(DENIED_EGRESS) <= set(block.get("except") or []) and block["cidr"].endswith("/0")
                ):
                    wrong.append(f"{label(obj)}: egress reaches {' or '.join(DENIED_EGRESS)}")
    assert sorted(app_namespaces - fenced) == [], "these app namespaces have no NetworkPolicy over all their pods"
    assert wrong == []


def test_network_policies_are_enforced():
    """NetworkPolicies do nothing unless Flannel runs its policy agent, which a control-plane patch turns on."""
    patches = (ROOT / "group_vars" / "kubernetes" / "talos.yml").read_text()
    assert "kind: KubeFlannelCNIConfig\n  kubeNetworkPoliciesEnabled: true" in patches


# --------------------------------------------------------------------------------------- app selection ---
# local.yml's kubernetes_apps names the app groups a host runs, out of group_vars/all/kubernetes.yml's
# kubernetes_app_groups; roles/flux_bootstrap drops the others' Flux Kustomizations from the sync, and Homepage shows a
# section for each group that runs.
def app_groups():
    return yaml.safe_load((ROOT / "group_vars" / "all" / "kubernetes.yml").read_text())["kubernetes_app_groups"]


def test_app_groups_are_the_groups_flux_applies():
    """Each app group is one Flux Kustomization, apps-<group>, applying k8s/apps/<group>, in both setups, and no other
    Flux Kustomization applies an app."""
    for setup, kustomizations in flux_kustomizations().items():
        apps = {name: path for name, (path, _) in kustomizations.items() if path.startswith("k8s/apps/")}
        assert apps == {f"apps-{group}": f"k8s/apps/{group}" for group in app_groups()}, setup


def test_homepage_has_a_section_for_every_group():
    """Homepage joins its services.yaml from homepage-tiles: a section for each group but its own, and Home
    Assistant's, which group_vars/kubernetes/flux.yml names in HOMEPAGE_SERVICES. Joined in that order, they are
    Homepage's list of sections."""
    names = [group for group in app_groups() if group != "homepage"] + ["home"]
    tiles = APPS / "homepage" / "tiles"
    assert sorted(p.name for p in tiles.iterdir()) == sorted(f"{n}.yaml.tmpl" for n in names)
    kustomization = yaml.safe_load((APPS / "homepage" / "kustomization.yaml").read_text())
    generator = next(g for g in kustomization["configMapGenerator"] if g["name"] == "homepage-tiles")
    assert sorted(generator["files"]) == sorted(f"{n}.yaml=tiles/{n}.yaml.tmpl" for n in names)
    sections = yaml.safe_load("".join((tiles / f"{n}.yaml.tmpl").read_text() for n in names))
    assert [next(iter(s)) for s in sections] == ["Media", "Cloud", "Photos", "Cameras", "Home"], sections
    assert all(isinstance(tile, dict) for s in sections for tile in next(iter(s.values()))), sections


# ----------------------------------------------------------------------------------------------- Renovate ---


def test_renovate_age_gate_lets_releases_without_a_timestamp_through():
    """Renovate 42 holds back, for good, a release whose registry gives no timestamp when minimumReleaseAge is set
    (minimumReleaseAgeBehaviour defaults to timestamp-required), and only Docker Hub gives one: every lscr.io and
    ghcr.io image and both OCI charts would never be updated. The age gate applies where a timestamp exists."""
    import json

    config = json.loads((ROOT / "renovate.json").read_text())
    if config.get("minimumReleaseAge"):
        assert config.get("minimumReleaseAgeBehaviour") == "timestamp-optional"


def test_renovate_parses_every_linuxserver_tag():
    """Renovate's versioning for each LinuxServer.io image: exactly one rule's package pattern matches it, and its tag
    parses with that rule's regex. Otherwise Renovate would propose no update for it, security releases included."""
    import json

    rules = json.loads((ROOT / "renovate.json").read_text())["packageRules"]
    parsed = [
        (
            re.compile(rule["matchPackageNames"][0].strip("/")),
            re.compile(rule["versioning"].removeprefix("regex:").replace("(?<", "(?P<")),
        )
        for rule in rules
        if rule.get("versioning", "").startswith("regex:")
    ]
    wrong = []
    for _, obj, spec in workloads():
        for container in containers(spec):
            image = container["image"].split("@")[0]
            if not LINUXSERVER.match(image):
                continue
            name, _, tag = image.rpartition(":")
            matching = [versioning for package, versioning in parsed if package.search(name)]
            if len(matching) != 1:
                wrong.append(f"{label(obj)}: {name} matches {len(matching)} rules")
            elif not matching[0].match(tag):
                wrong.append(f"{label(obj)}: {tag} does not parse as its rule's versioning")
    assert wrong == []
