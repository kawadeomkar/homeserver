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
  render (make lint-helm), the ones listed for them.
- The apps (k8s/apps/) run one replica each, replaced whole (Recreate), Homepage aside; every container refuses
  privilege escalation under the RuntimeDefault seccomp profile; every Ingress uses Traefik and the domain setting;
  every pod that mounts a shared NAS or static volume chowns its mount point first, and none sets fsGroup on an NFS
  volume, which would chown the whole volume at every start.
- No PodDisruptionBudget: with one node for the apps, it would only stop a drain.
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
from helpers import CLUSTER_ARGS, PROXMOX_ONLY_CLUSTER_LOCAL_YML, example_tree, resolve_on

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "k8s"
CLUSTERS = K8S / "clusters"
# What make lint-helm rendered the charts into (its HELM_RENDERED), .cache/helm-rendered by default.
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
# what make lint-helm renders. The others' need shows in the repository's own manifests (Intel's GPU plugin, vendored;
# the Job that makes the static volumes' directories in local-path-storage).
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

# The hostPath volumes in what the charts render (make lint-helm): a chart bump that adds one fails here before it
# merges. csi-driver-nfs mounts the kubelet's directories, in kube-system, which Talos exempts from Pod Security;
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
        and not str(obj.get("apiVersion", "")).startswith("kustomize.config")
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
                # One applying another repository's files (Intel's GPU plugin) has nothing here to follow.
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
    out of every check: make lint-kube-linter lints the substituted manifests, and this makes sure they parse."""
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
    """What make lint-helm rendered, as (path, object); empty when it has not run (make test, offline)."""
    if not RENDERED.is_dir():
        return ()
    found = []
    for path in sorted(RENDERED.rglob("*.yaml")):
        found.extend(
            (path.relative_to(ROOT), obj) for obj in yaml.safe_load_all(path.read_text()) if obj and obj.get("kind")
        )
    return tuple(found)


@pytest.mark.skipif(not RENDERED.is_dir(), reason="make lint-helm renders the charts first")
def test_chart_host_paths_are_pinned():
    found = set().union(*(host_paths(obj) for _, obj in rendered()))
    assert sorted(found - CHART_HOST_PATHS) == [], "a chart mounts a new hostPath: check why, then pin it here"
    assert sorted(CHART_HOST_PATHS - found) == [], "these are gone from the charts: remove them from the list"


@pytest.mark.skipif(not RENDERED.is_dir(), reason="make lint-helm renders the charts first")
def test_charts_open_no_node_port():
    """Traefik's LoadBalancer comes from its chart: a chart bump that starts allocating node ports fails here."""
    assert node_port_services(rendered()) == [], "set the chart's value that keeps node ports off"


@pytest.mark.skipif(not RENDERED.is_dir(), reason="make lint-helm renders the charts first")
def test_chart_privileged_namespaces_are_still_needed():
    """A namespace labelled privileged for a chart's sake still holds a rendered pod that needs it: a chart bump that
    drops node-exporter's host namespaces must not leave monitoring privileged for good."""
    needed = {namespace(obj) for _, obj, spec in workloads(rendered()) if beyond_baseline(spec) or host_paths(obj)}
    assert sorted(CHART_PRIVILEGED - needed) == [], (
        "no chart's pod needs the label any more: drop it, and from the list"
    )


@pytest.mark.skipif(not RENDERED.is_dir(), reason="make lint-helm renders the charts first")
def test_chart_privileged_pods_only_in_privileged_namespaces():
    allowed = PRIVILEGED_NAMESPACES | SYSTEM_NAMESPACES
    wrong = {
        f"{path}: {label(obj)}": found
        for path, obj, spec in workloads(rendered())
        if (found := beyond_baseline(spec)) and namespace(obj) not in allowed
    }
    assert wrong == {}, "a chart's pod needs more than baseline: label its namespace, saying why"


def test_chart_fixtures_match_the_settings(settings):
    """make lint-helm renders the charts with tests/fixtures/cluster-settings.yaml's placeholders; each setup there has
    exactly the keys roles/flux_bootstrap puts in that setup's cluster-settings."""
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

APPS = K8S / "apps"
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


def test_ingresses_use_traefik_and_the_domain():
    wrong = []
    for _, obj in of_kind("Ingress"):
        spec = obj["spec"]
        if spec.get("ingressClassName") != "traefik":
            wrong.append(f"{label(obj)}: ingressClassName is not traefik")
        for rule in spec.get("rules") or []:
            if not rule.get("host", "").endswith("${DOMAIN}"):
                wrong.append(f"{label(obj)}: host {rule.get('host')} is not <app>.${{DOMAIN}}")
        if any(a.startswith("nginx.ingress.kubernetes.io/") for a in (obj["metadata"].get("annotations") or {})):
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
