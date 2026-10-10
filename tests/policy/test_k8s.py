"""The Kubernetes manifests keep the conventions CLAUDE.md sets for them that kube-linter cannot check.

- Every port a Service opens on the nodes names its nodePort, in the default range, and no two Services share
  one. The ports Kubernetes would pick itself are listed below, and the list only shrinks.
- Each namespace is declared once: in its directory's 0-namespace.yaml, or in k8s/00-namespaces/ when more
  than one directory uses it. Every namespaced object names its namespace, which is its directory's, a
  shared one or kube-system, so `kubectl apply` never falls back to the current context's.
- The namespaces labelled for privileged pods are exactly the ones listed below, each label says why in a
  comment, and each namespace still holds a pod that needs it.
- Only those namespaces and kube-system hold pods that Pod Security Admission's baseline level rejects for
  the privilege they ask for: privileged containers, added capabilities, host namespaces and host ports.
- The hostPath volumes, in pods and in PersistentVolumes, are exactly the ones listed below.
- Every Secret is SOPS-encrypted, apart from service-account token stubs, which hold no data.
- LinuxServer.io images use the immutable x.y.z-lsNNN form of their tags.

kube-linter (.kube-linter.yaml) covers the rest: privileged containers and added capabilities without an
annotation saying why, unpinned images, imagePullPolicy Always, and missing CPU and memory requests.
"""

import functools
import re
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "k8s"
SHARED = K8S / "00-namespaces"

# Pod Security Admission exempts these, so they need no label. Talos exempts kube-system by default.
SYSTEM_NAMESPACES = {"kube-system"}

# The namespaces labelled pod-security.kubernetes.io/enforce: privileged. Changing the set needs a reason in
# the pull request, and the label a comment saying why.
PRIVILEGED_NAMESPACES = {"frigate", "homeassistant", "jellyfin", "monitoring"}

# The ports Kubernetes assigns a nodePort to itself, as (namespace, Service, port, protocol). Each one makes
# CLAUDE.md's NodePort list incomplete. Remove an entry when its port names a nodePort, or when its
# LoadBalancer Service sets allocateLoadBalancerNodePorts: false.
IMPLICIT_NODE_PORTS = {
    ("frigate", "frigate", 8554, "TCP"),
    ("frigate", "frigate", 8555, "TCP"),
    ("frigate", "frigate", 8555, "UDP"),
    ("jellyfin", "jellyfin-lb-tcp", 8096, "TCP"),
    ("jellyfin", "jellyfin-lb-tcp", 8920, "TCP"),
    ("jellyfin", "jellyfin-lb-udp", 1900, "UDP"),
    ("jellyfin", "jellyfin-lb-udp", 7359, "UDP"),
    ("nextcloud", "nextcloud-service", 443, "TCP"),
}

# Every hostPath volume, as (object, path). Pod Security Admission's baseline level rejects one in a pod
# spec, but does not look inside a PersistentVolume; Talos's root filesystem is immutable either way. Each
# is debt for the conversion to PersistentVolumeClaims (CLAUDE.md, storage model): remove an entry when its
# volume is converted. Adding one needs a reason in the pull request.
HOST_PATHS = {
    ("DaemonSet kube-system/node-exporter", "/"),
    ("Deployment dashy/dashy", "/opt/dashy/config.yml"),
    ("Deployment dashy/dashy", "/opt/dashy/item-icons"),
    ("Deployment filebrowser/filebrowser", "/opt/filebrowser/filebrowser.db"),
    ("Deployment filebrowser/filebrowser", "/opt/media/data"),
    ("Deployment frigate/frigate", "/dev/dri"),
    ("Deployment frigate/frigate", "/etc/localtime"),
    ("Deployment frigate/frigate", "/opt/frigate-db"),
    ("Deployment frigate/frigate", "/opt/frigate/config.yml"),
    ("Deployment frigate/frigate", "/opt/frigate/storage"),
    ("Deployment homarr/homarr", "/opt/homarr/configs"),
    ("Deployment homarr/homarr", "/opt/homarr/data"),
    ("Deployment homarr/homarr", "/opt/homarr/icons"),
    ("Deployment homeassistant/homeassistant-deployment", "/etc/localtime"),
    ("Deployment jellyfin/jellyfin", "/dev/dri/renderD128"),
    ("Deployment jellyseerr/jellyseerr", "/opt/jellyseerr/config"),
    ("Deployment prowlarr/prowlarr", "/opt/prowlarr/config"),
    ("Deployment radarr/radarr", "/opt/media/data"),
    ("Deployment radarr/radarr", "/opt/radarr/config"),
    ("Deployment sabnzbd/sabnzbd", "/opt/media/data"),
    ("Deployment sabnzbd/sabnzbd", "/opt/media/data/incomplete"),
    ("Deployment sabnzbd/sabnzbd", "/opt/sabnzbd/config"),
    ("Deployment sonarr/sonarr", "/opt/media/data"),
    ("Deployment sonarr/sonarr", "/opt/sonarr/config"),
    ("PersistentVolume grafana-opt-volume", "/opt/grafana"),
    ("PersistentVolume prometheus-storage-volume", "/opt/prometheus"),
}

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


@functools.cache
def manifests():
    """Every object under k8s/, as (path relative to the repository, object). Comment-only files hold none."""
    found = []
    for path in sorted(K8S.rglob("*.y*ml")):
        found.extend((path.relative_to(ROOT), obj) for obj in yaml.safe_load_all(path.read_text()) if obj)
    assert len(found) > 50, "the manifests were not found"
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


def workloads():
    return [(path, obj, pod_spec(obj)) for path, obj in manifests() if obj["kind"] in POD_SPEC]


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
    for container in containers(spec):
        context = container.get("securityContext") or {}
        name = container["name"]
        if context.get("privileged"):
            found.append(f"{name}: privileged")
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


def node_ports():
    """{(namespace, Service, port, protocol): nodePort, or None if unset} for each port a Service opens on the nodes."""
    ports = {}
    for _, service in of_kind("Service"):
        spec = service["spec"]
        kind = spec.get("type", "ClusterIP")
        if kind == "NodePort" or (kind == "LoadBalancer" and spec.get("allocateLoadBalancerNodePorts", True)):
            for port in spec["ports"]:
                key = (namespace(service), service["metadata"]["name"], port["port"], port.get("protocol", "TCP"))
                ports[key] = port.get("nodePort")
    return ports


def test_node_ports_are_explicit_and_unique():
    ports = node_ports()
    assert ports, "no Service opens a port on the nodes"
    implicit = {key for key, number in ports.items() if number is None}
    assert implicit - IMPLICIT_NODE_PORTS == set(), "name a nodePort that no other Service uses (CLAUDE.md lists them)"
    assert IMPLICIT_NODE_PORTS - implicit == set(), "these now name a nodePort or are gone: remove them from the list"
    assert {key: number for key, number in ports.items() if number is not None and not 30000 <= number <= 32767} == {}
    owners = defaultdict(set)
    for (space, service, _, _), number in ports.items():
        if number is not None:
            owners[number].add(f"{space}/{service}")
    assert {number: services for number, services in owners.items() if len(services) > 1} == {}


def test_each_namespace_is_declared_once_by_its_owner():
    declared = defaultdict(list)
    for path, obj in of_kind("Namespace"):
        declared[obj["metadata"]["name"]].append(path)
    users = defaultdict(set)
    for path, obj in manifests():
        if obj["kind"] not in CLUSTER_SCOPED:
            users[namespace(obj)].add(path.parent.name)
    wrong = [f"{name} is declared {len(paths)} times" for name, paths in declared.items() if len(paths) > 1]
    # An object without a namespace fails the next test.
    for name, directories in users.items():
        if name is None or name in SYSTEM_NAMESPACES:
            continue
        if len(directories) > 1:
            owner = "k8s/00-namespaces/"
            owned = [path for path in declared[name] if path.parent == SHARED.relative_to(ROOT)]
        else:
            owner = f"k8s/{min(directories)}/0-namespace.yaml"
            owned = [path for path in declared[name] if str(path) == owner]
        if not owned:
            wrong.append(f"{name}, used by {', '.join(sorted(directories))}, must be declared in {owner}")
    assert wrong == []


def test_namespaced_objects_name_their_namespace():
    shared = {obj["metadata"]["name"] for path, obj in of_kind("Namespace") if path.parent == SHARED.relative_to(ROOT)}
    wrong = [
        f"{path}: {label(obj)}"
        for path, obj in manifests()
        if obj["kind"] not in CLUSTER_SCOPED and namespace(obj) not in {path.parent.name, *shared, *SYSTEM_NAMESPACES}
    ]
    assert wrong == [], "a namespaced object belongs in its directory's namespace, a shared one, or kube-system"


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
    assert PRIVILEGED_NAMESPACES - needed == set(), "nothing in these needs the label any more: remove it"


def test_privileged_pods_only_in_privileged_namespaces():
    allowed = PRIVILEGED_NAMESPACES | SYSTEM_NAMESPACES
    wrong = {
        label(obj): found
        for _, obj, spec in workloads()
        if (found := beyond_baseline(spec)) and namespace(obj) not in allowed
    }
    assert wrong == {}, "Talos enforces baseline: label the namespace and say why (CLAUDE.md, adding a service)"


def test_host_paths_are_pinned():
    found = set().union(*(host_paths(obj) for _, obj in manifests()))
    assert sorted(found - HOST_PATHS) == [], "use a PersistentVolumeClaim, not a new hostPath (CLAUDE.md)"
    assert sorted(HOST_PATHS - found) == [], "these are gone: remove them from the list"


def test_secrets_are_sops_encrypted():
    plaintext = [
        f"{path}: {label(obj)}"
        for path, obj in of_kind("Secret")
        if "sops" not in obj
        and not (
            obj.get("type") == "kubernetes.io/service-account-token"
            and not obj.get("data")
            and not obj.get("stringData")
        )
    ]
    assert plaintext == [], "encrypt it with SOPS (k8s/README-secrets.md), or create it by hand"


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
