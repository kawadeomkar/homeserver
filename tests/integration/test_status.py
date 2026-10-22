"""Runs make status, the real status.yml with its callback and make's exit codes, against the fake Proxmox host, the
fake cluster and the fake NAS.

The playbook, roles, group_vars and Makefile are copied into a scratch tree with a local.yml of a Proxmox-only setup
with a cluster and Home Assistant, as the policy tests do. The Proxmox host is reached locally with the fake's
commands first on PATH, and the cluster through fake_talos.py's kubectl, talosctl and git, with credentials of its own
in a directory under the test's own HOME, so no run ever reads the workstation's. Checked: the report's file and
text, the callback's output (one line on what was read, then the report, and no task banners), that nothing on the
host or in the cluster changed, the apps and their states, the nodes' memory and disks from kubelet in place of
Proxmox's view, the exit codes (make's own, 0 or 2 as for any failed recipe, and the report's, 1 for a problem, 2 for
a source that could not be read, 3 for a refused run, on the report's last line and in make's "Error N"), JSON=1,
another cluster's kubeconfig, and a host that does not answer at all. With a NAS (local.yml.example's setup), the NAS
is read through roles/truenas's status entry point against fake_truenas.py: one login, no change, no secret in the
report, and a NAS that does not answer or presents another certificate is a source not read. The apps' probes ask
addresses that never answer here, under a domain that never resolves (.invalid), so what they find does not depend on
the network.

Run: make test TYPE=idempotence
"""

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests" / "policy"))
import fake_pve  # noqa: E402
import fake_talos  # noqa: E402
import fake_truenas  # noqa: E402
from helpers import (  # noqa: E402
    CLUSTER_VARS,
    PROXMOX_ONLY_CLUSTER_LOCAL_YML,
    PROXMOX_ONLY_LOCAL_YML,
    example_tree,
    example_without_kubernetes,
)

GIB = 2**30
BIN = Path(sys.executable).parent
PINS = yaml.safe_load((ROOT / "group_vars" / "all" / "kubernetes.yml").read_text())
CONTROL_PLANE = "192.0.2.30"
TRAEFIK = "192.0.2.40"
COMMIT = "7821488ed966940b57a3a7907bcad8109e76a867"
FLUX_LABELS = "kustomize.toolkit.fluxcd.io"
HAOS_FSINFO = [
    {"mountpoint": "/mnt/data", "type": "ext4", "used-bytes": 5 * GIB, "total-bytes": 30 * GIB, "disk": []},
    {"mountpoint": "/", "type": "erofs", "used-bytes": GIB // 4, "total-bytes": GIB // 4, "disk": []},
]


def at(**delta):
    """An RFC 3339 time this far from now."""
    return (datetime.now(UTC) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def crd(kind, plural, group):
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": f"{plural}.{group}"},
        "spec": {"names": {"kind": kind}, "scope": "Namespaced"},
    }


def deployment(namespace, name, image, owner="", replicas=1, labels=None, annotations=None, managed=None, app=""):
    """A Deployment as Flux applies it from k8s/apps (`owner` its Kustomization), or a chart's (no owner), its pods
    labelled with `app` (its own name otherwise)."""
    flux = {f"{FLUX_LABELS}/name": owner, f"{FLUX_LABELS}/namespace": "flux-system"} if owner else {}
    selector = {"app.kubernetes.io/name": app or name}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {**selector, **flux, **(labels or {})},
            "annotations": annotations or {},
            "managedFields": managed or [],
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            "template": {"metadata": {"labels": selector}, "spec": {"containers": [{"name": name, "image": image}]}},
        },
    }


def service(namespace, name, selector, kind="ClusterIP", address=""):
    ingress = [{"ip": address, "ipMode": "VIP"}] if address else []
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"type": kind, "selector": selector, "ports": [{"name": "http", "port": 80}]},
        "status": {"loadBalancer": {"ingress": ingress}},
    }


def ingress(namespace, name, host, backend):
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "ingressClassName": "traefik",
            "rules": [
                {
                    "host": host,
                    "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {"service": {"name": backend}}}]},
                }
            ],
        },
        "status": {"loadBalancer": {"ingress": [{"ip": TRAEFIK}]}},
    }


def ready(kind, namespace, name, spec, status=None, ok=True):
    condition = {"type": "Ready", "status": "True" if ok else "False", "reason": "Succeeded", "message": "Applied"}
    if not ok:
        condition |= {"reason": "BuildFailed", "message": "kustomize build failed: accumulating resources"}
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
        "status": {"conditions": [condition], **(status or {})},
    }


def node(name, control_plane=False):
    labels = {
        "kubernetes.io/hostname": name,
        **({"node-role.kubernetes.io/control-plane": ""} if control_plane else {}),
    }
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "False"}],
            "nodeInfo": {
                "osImage": f"Talos ({PINS['talos_version']})",
                "kubeletVersion": "v" + PINS["talos_kubernetes_version"],
            },
            "allocatable": {"cpu": "3950m", "memory": "7033492Ki", "pods": "110"},
        },
    }


def local_volume(claim, path):
    namespace, name = claim.split("/")
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": f"pvc-{name}"},
        "spec": {"local": {"path": path}, "claimRef": {"namespace": namespace, "name": name}},
    }


def summary(memory_used, memory_available, var_used, var_total, volumes=()):
    """kubelet's statistics for a node, as /stats/summary gives them, its pods' volumes each ((namespace, claim),
    used, capacity)."""
    return {
        "node": {
            "memory": {"workingSetBytes": memory_used, "availableBytes": memory_available},
            "fs": {"usedBytes": var_used, "capacityBytes": var_total, "availableBytes": var_total - var_used},
        },
        "pods": [
            {
                "podRef": {"namespace": claim[0], "name": f"{claim[1]}-pod"},
                "volume": [
                    {
                        "name": "data",
                        "pvcRef": {"namespace": claim[0], "name": claim[1]},
                        "usedBytes": used,
                        "capacityBytes": capacity,
                    }
                ],
            }
            for claim, used, capacity in volumes
        ],
    }


def seed_cluster(path, domain="homeserver.invalid", **more):
    """The cluster as Flux leaves it: Homepage, Jellyfin, Sonarr (suspended by make suspend two days ago), Nextcloud
    and its database, and Grafana from its chart; every Kustomization applied at the commit the branch points at,
    Traefik's allowlist still closed, nothing firing but the Watchdog."""
    data = "/var/mnt/local-path-provisioner"
    suspended = at(days=-2)
    objects = [
        *fake_kubectl_operator(),
        crd("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io"),
        crd("Certificate", "certificates", "cert-manager.io"),
        node("homeserver-cp-1", control_plane=True),
        node("homeserver-worker-1"),
        deployment("homepage", "homepage", "ghcr.io/gethomepage/homepage:v1.13.2", "apps-homepage", replicas=2),
        deployment("media", "jellyfin", "docker.io/linuxserver/jellyfin:10.11.11ubu2404-ls42", "apps-media"),
        deployment(
            "media",
            "sonarr",
            "docker.io/linuxserver/sonarr:4.0.19.2979-ls320",
            "apps-media",
            replicas=0,
            annotations={f"{FLUX_LABELS}/reconcile": "disabled", "homeserver.internal/replicas": "1"},
            managed=[
                {
                    "manager": "homeserver-ansible",
                    "operation": "Update",
                    "time": suspended,
                    "fieldsV1": {"f:metadata": {"f:annotations": {"f:homeserver.internal/replicas": {}}}},
                }
            ],
        ),
        deployment("nextcloud", "nextcloud", "docker.io/linuxserver/nextcloud:34.0.2-ls444", "apps-nextcloud"),
        deployment(
            "nextcloud",
            "nextcloud-db",
            "docker.io/linuxserver/mariadb:11.4.12-r0-ls224",
            "apps-nextcloud",
            labels={"app.kubernetes.io/component": "database"},
        ),
        deployment(
            "monitoring",
            "kube-prometheus-stack-grafana",
            "quay.io/kiwigrid/k8s-sidecar:2.11.2",
            labels={"app.kubernetes.io/version": "13.2.3"},
            app="grafana",
        ),
        service("homepage", "homepage", {"app.kubernetes.io/name": "homepage"}),
        service("media", "jellyfin", {"app.kubernetes.io/name": "jellyfin"}),
        service("media", "sonarr", {"app.kubernetes.io/name": "sonarr"}),
        service("nextcloud", "nextcloud", {"app.kubernetes.io/name": "nextcloud"}),
        service("monitoring", "kube-prometheus-stack-grafana", {"app.kubernetes.io/name": "grafana"}),
        service("traefik", "traefik", {"app.kubernetes.io/name": "traefik"}, kind="LoadBalancer", address=TRAEFIK),
        ingress("homepage", "homepage", domain, "homepage"),
        ingress("media", "jellyfin", f"jellyfin.{domain}", "jellyfin"),
        ingress("media", "sonarr", f"sonarr.{domain}", "sonarr"),
        ingress("nextcloud", "nextcloud", f"nextcloud.{domain}", "nextcloud"),
        ingress("monitoring", "kube-prometheus-stack-grafana", f"grafana.{domain}", "kube-prometheus-stack-grafana"),
        local_volume("media/jellyfin-config", f"{data}/pvc-1_media_jellyfin-config"),
        local_volume("nextcloud/nextcloud-config", f"{data}/pvc-2_nextcloud_nextcloud-config"),
        local_volume("media/media", "/var/mnt/media/static/media"),
        ready(
            "FluxInstance",
            "flux-system",
            "flux",
            {"sync": {"ref": "refs/heads/main", "path": "k8s/clusters/local"}},
            {"lastAppliedRevision": "v2.9.6@sha256:c7b9"},
        ),
        *(
            ready(
                "Kustomization",
                "flux-system",
                name,
                {"path": path},
                {"lastAppliedRevision": f"refs/heads/main@sha1:{COMMIT}"},
            )
            for name, path in [
                ("flux-system", "k8s/clusters/local"),
                ("infra-controllers", "./k8s/infrastructure/controllers/local"),
                ("apps-homepage", "./k8s/apps/homepage"),
                ("apps-media", "./k8s/apps/media"),
                ("apps-nextcloud", "./k8s/apps/nextcloud"),
            ]
        ),
        ready("HelmRelease", "monitoring", "kube-prometheus-stack", {}),
        ready(
            "Certificate",
            "traefik",
            "wildcard",
            {"dnsNames": [domain, f"*.{domain}"]},
            {"notAfter": at(days=89), "renewalTime": at(days=59)},
        ),
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "cluster-settings", "namespace": "flux-system"},
            "data": {"DOMAIN": domain, "INGRESS_ALLOWED_RANGE": "127.0.0.1/32"},
        },
    ]
    state = {
        "bootstrapped": True,
        "nodes": {CONTROL_PLANE: {"mode": "configured"}},
        "objects": {fake_talos.fake_kubectl.key(obj): obj for obj in objects},
        "pods_node": "homeserver-worker-1",
        "node_stats": {
            "homeserver-cp-1": summary(2 * GIB, 2 * GIB, 3 * GIB, 30 * GIB),
            "homeserver-worker-1": summary(
                5 * GIB,
                7 * GIB,
                9 * GIB,
                30 * GIB,
                [
                    (("media", "jellyfin-config"), 4 * GIB, 20 * GIB),
                    (("nextcloud", "nextcloud-config"), 4 * GIB, 20 * GIB),
                    (("media", "media"), 30 * GIB, 100 * GIB),
                ],
            ),
        },
        "alerts": [{"labels": {"alertname": "Watchdog", "severity": "none"}, "status": {"state": "active"}}],
        "remote_heads": {"refs/heads/main": COMMIT},
        "mutations": 0,
    }
    state.update(more)
    path.write_text(json.dumps(state))
    return state


def fake_kubectl_operator():
    """The Flux Operator's own objects, as its release installs them: its CRDs among them."""
    return fake_talos.fake_kubectl.operator_objects("v0.61.0")


def talos_dir(tmp_path):
    """Where roles/talos keeps the cluster's credentials and etcd snapshots, under the test's own HOME."""
    return tmp_path / "home" / ".talos" / "homeserver"


def credentials(directory, server=f"https://{CONTROL_PLANE}:6443", days=364):
    """A kubeconfig naming `server` and a talosconfig for the control plane, each with a real client certificate."""
    directory.mkdir(parents=True, exist_ok=True)
    crt, key = fake_talos.certificate(days)
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "homeserver", "cluster": {"server": server}}],
        "users": [
            {
                "name": "admin",
                "user": {"client-certificate-data": fake_talos.b64(crt), "client-key-data": fake_talos.b64(key)},
            }
        ],
        "contexts": [{"name": "admin", "context": {"cluster": "homeserver", "user": "admin"}}],
        "current-context": "admin",
    }
    talosconfig = {
        "context": "homeserver",
        "contexts": {
            "homeserver": {
                "endpoints": [CONTROL_PLANE],
                "nodes": [CONTROL_PLANE],
                "crt": fake_talos.b64(crt),
                "key": fake_talos.b64(key),
            }
        },
    }
    for name, config in (("kubeconfig", kubeconfig), ("talosconfig", talosconfig)):
        (directory / name).write_text(yaml.safe_dump(config))
        (directory / name).chmod(0o600)


def vm(name, cores, memory_mib, status="running", **more):
    return {"config": {"name": name, "cores": cores, "memory": memory_mib, **more}, "pending": {}, "status": status}


def seed(path, **more):
    """A Proxmox-only host as this setup leaves it: the two nodes and Home Assistant running, another project's VM
    and a template beside them, a restart and an upgrade waiting, and Home Assistant's guest agent answering."""
    state = fake_pve.fresh_state()
    state["storage_total"] = {"local-lvm": 200 * GIB}
    state["vms"] = {
        "300": vm("homeserver-cp-1", 2, 4096),
        "301": vm("homeserver-worker-1", 4, 12288),
        "310": vm("homeassistant", 2, 4096),
        "9000": vm("ubuntu-template", 2, 2048, status="stopped", template=1),
        "9001": vm("another-project", 2, 4096),
    }
    state["vms"]["301"]["pending"] = {"memory": 16384}
    state["content"] = {
        "local-lvm": [
            {"volid": f"local-lvm:vm-{vmid}-disk-{n}", "vmid": vmid, "content": "images", "size": size * GIB}
            for vmid, n, size in [(300, 0, 32), (301, 0, 32), (301, 1, 20), (301, 2, 100), (310, 0, 32)]
        ]
    }
    state["guest_fsinfo"] = {"310": HAOS_FSINFO}
    state["guest_osinfo"] = {"310": {"id": "haos", "pretty-name": "Home Assistant OS 18.3", "version-id": "18.3"}}
    state["usage"] = {"301": {"cpu": 0.42, "mem": 9 * GIB, "uptime": 5 * 86400}}
    state["guest_ip"] = "192.0.2.32"
    state["packages"]["lists"] = dict(state["packages"]["repository"])
    state.update(more)
    path.write_text(json.dumps(state))
    return state


@pytest.fixture
def tree(tmp_path):
    """The repository's playbooks, roles, group_vars, callback and Makefile, with a Proxmox-only cluster's local.yml,
    the cluster's credentials in a directory of their own, and the fake cluster's state beside the fake host's."""
    root = example_tree(tmp_path / "tree", PROXMOX_ONLY_CLUSTER_LOCAL_YML)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "Makefile", root)
    (root / "run").mkdir()
    (tmp_path / "home").mkdir()
    credentials(talos_dir(tmp_path))
    seed_cluster(tmp_path / "talos.json")
    return root


# What make passes a make run inside it: make test TYPE=idempotence's own TYPE would reach make status through these.
MAKE_VARIABLES = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES", "TYPE", "JSON"}


def environment(tree, state, fake=True, **more):
    """The fakes first on PATH (without fake_pve's, when not `fake`: a host without Proxmox's commands), each with
    its state, and HOME the test's own, so that nothing reads the workstation's credentials. SSH shares no
    connection, whose socket's path under that HOME would be too long. Nothing of the make that runs the tests."""
    fakes = [str(HERE / "fake_talos_bin"), *([str(HERE / "fake_pve_bin")] if fake else [])]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANSIBLE_", "STATUS_", "NO_COLOR")) and k not in MAKE_VARIABLES
    }
    return env | {
        "ANSIBLE_CONFIG": str(tree / "ansible.cfg"),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver_status",
        "ANSIBLE_SSH_ARGS": "-o ControlMaster=no",
        "PATH": ":".join([*fakes, str(BIN), os.environ["PATH"]]),
        "HOME": str(state.parent / "home"),
        "FAKE_PVE_STATE": str(state),
        "FAKE_PVE_PYTHON": sys.executable,
        "FAKE_TALOS_STATE": str(state.parent / "talos.json"),
        "FAKE_TALOS_PYTHON": sys.executable,
        **more,
    }


def extra_vars(tree, local=True, cluster=True, **more):
    """The variables a run needs here: the local connection, unless the inventory gives one, this Python, the
    placeholders' values (a cluster's and Home Assistant's, unless the setup has neither), the reboot flag in the tree,
    the cluster's credentials, and no probes unless asked."""
    return json.dumps(
        {
            **({"ansible_connection": "local"} if local else {}),
            "ansible_python_interpreter": sys.executable,
            "status_reboot_required_file": str(tree / "run" / "reboot-required"),
            "talos_config_dir": str(talos_dir(tree.parent)),
            "status_probe_apps": False,
            **(CLUSTER_VARS if cluster else {}),
            **more,
        }
    )


def playbook(tree, state, *args, **env):
    cmd = [str(BIN / "ansible-playbook"), "-i", "inventory.example", "status.yml", "--check", *args]
    proc = subprocess.run(
        cmd, cwd=tree, env=environment(tree, state, **env), capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_status(tree, state, *args, fake=True, inventory="inventory.example", local=True, cluster=True, **more):
    """make status in the tree, with this test's inventory and variables passed through ANSIBLE_ARGS."""
    ansible_args = f"-i {inventory} -e '{extra_vars(tree, local, cluster, **more)}'"
    cmd = ["make", "--no-print-directory", "status", f"VENV={sys.prefix}", f"ANSIBLE_ARGS={ansible_args}", *args]
    env = environment(tree, state, fake=fake)
    env.pop("ANSIBLE_STDOUT_CALLBACK")
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def report_of(tree):
    return json.loads((tree / ".cache" / "status" / "report.json").read_text())


def make_failed(err, code):
    """Whether make said its status recipe failed with `code`: "[status] Error 2" from GNU make 3.81 (macOS's),
    "[Makefile:415: status] Error 2" from 4.x (Linux's)."""
    return re.search(rf"\[(?:[^]\s]+:\d+: )?status\] Error {code}\b", err) is not None


def test_the_report_of_a_proxmox_only_host_and_its_cluster(tree, tmp_path):
    state = tmp_path / "pve.json"
    before = seed(state)
    cluster_before = json.loads((tmp_path / "talos.json").read_text())
    (tree / "run" / "reboot-required").write_text("*** System restart required ***\n")
    rc, out, err = playbook(tree, state, "-e", extra_vars(tree))
    assert rc == 0, out + err

    # Nothing on the host or in the cluster changed: every command was a read.
    after = json.loads(state.read_text())
    assert after["mutations"] == before["mutations"] and after["vms"] == before["vms"]
    cluster_after = json.loads((tmp_path / "talos.json").read_text())
    assert cluster_after["objects"] == cluster_before["objects"] and cluster_after["mutations"] == 0

    report = report_of(tree)
    assert stat.S_IMODE((tree / ".cache" / "status" / "report.json").stat().st_mode) == 0o600
    assert report["setup"] == {"nas": False, "cluster": True, "home_assistant": True}
    assert report["exit"] == 0, report["attention"]
    assert [(a["subject"], a["command"]) for a in report["attention"]] == [
        ("suspended", "make resume ONLY=sonarr"),
        (
            "allowlist",
            "the first visits (scripts/runbooks/first-run.md), then kubernetes_ingress_allowed_range in local.yml "
            "and make deploy TYPE=kubernetes",
        ),
        ("reboot", "reboot it at a quiet time"),
        ("upgrades", "make deploy TYPE=proxmox"),
        ("restart", "qm reboot 301"),
        # The disks provisioned on local-lvm add up to more than it holds, as on a host run with overcommit allowed.
        ("local-lvm", "keep its use well below 100% (pvesm status)"),
        # Without a NAS and without vm_backup_storage there is no backup job.
        (
            "backup",
            "vm_backup_storage in local.yml names where they go (a USB disk, or a Proxmox Backup Server), then make "
            "deploy TYPE=proxmox",
        ),
    ]
    assert report["attention"][0]["text"] == f"sonarr is suspended since {at(days=-2)[:10]}"
    vms = {v["vmid"]: v for v in report["proxmox"]["vms"]}
    assert sorted(vms) == [300, 301, 310]
    assert vms[301]["pending"] == ["memory"] and vms[301]["cpu"] == 0.42
    assert vms[310]["filesystems"] == [{"mount": "/mnt/data", "used": 5 * GIB, "total": 30 * GIB}]
    assert vms[310]["os"] == "Home Assistant OS 18.3"
    # The nodes' memory and disks are the guest's own, from kubelet, not Proxmox's view.
    assert vms[301]["memory"] == {"used": 5 * GIB, "total": 12 * GIB, "view": "guest"}
    assert vms[301]["filesystems"] == [
        {"mount": "/var", "used": 9 * GIB, "total": 30 * GIB},
        {"mount": "data", "used": 4 * GIB, "total": 20 * GIB},
        {"mount": "media", "used": 30 * GIB, "total": 100 * GIB},
    ]
    others = {"vmids": [9001], "templates": [9000], "running": 1, "cores": 2, "memory": 4 * GIB}
    assert report["proxmox"]["others"] == others
    budget = next(s for s in report["proxmox"]["storages"] if s["id"] == "local-lvm")["budget"]
    assert budget == {"provisioned": 216 * GIB, "limit": 200 * GIB}
    assert [v["id"] for v in report["volumes"]] == ["homeserver-worker-1 data", "homeserver-worker-1 media"]

    # The apps: each Ingress's, Grafana's from its chart among them, Nextcloud's database under it, Home Assistant's
    # VM at the address its guest agent reports.
    apps = {a["name"]: a for a in report["apps"]}
    assert list(apps) == ["homepage", "jellyfin", "sonarr", "grafana", "nextcloud", "homeassistant"]
    assert {name: a["state"] for name, a in apps.items()} == {
        "homepage": "running",
        "jellyfin": "running",
        "sonarr": "suspended",
        "grafana": "running",
        "nextcloud": "running",
        "homeassistant": "running",
    }
    assert apps["jellyfin"]["version"] == "10.11.11" and apps["grafana"]["version"] == "13.2.3"
    assert apps["jellyfin"]["urls"] == [{"url": "https://jellyfin.homeserver.invalid", "http": None, "error": ""}]
    assert [d["name"] for d in apps["nextcloud"]["databases"]] == ["nextcloud-db"]
    assert apps["homeassistant"]["urls"][0]["url"] == "http://192.0.2.32:8123"
    cluster = report["cluster"]
    assert [n["name"] for n in cluster["nodes"] if n["ready"]] == ["homeserver-cp-1", "homeserver-worker-1"]
    assert cluster["etcd"] == {"state": "Running", "health": "OK"}
    assert cluster["flux"]["applied"] == cluster["flux"]["remote"] == COMMIT
    assert cluster["alerts"] == [] and cluster["access"]["kubeconfig_days"] in (363, 364)

    # The callback: what was read, then the report, and nothing of Ansible's own.
    lines = out.splitlines()
    assert lines[0].startswith("reading  ") and "proxmox ok " in lines[0] and "cluster ok " in lines[0], out
    # The NAS's host reads nothing without a NAS, so the line leaves it out.
    assert " nas " not in lines[0] and "NAS  " not in out, out
    assert "TASK [" not in out and "PLAY RECAP" not in out and "\033[" not in out, out
    assert "VMS  pve · Proxmox VE 9.2.2-1 · kernel 7.0.2-6-pve" in out
    assert "+ 1 other VM (9001), 2 cores, 4.0 GiB, 1 running and 1 template (9000), not this repo's" in out
    assert "disks 216 of 200 GiB budgeted" in out
    assert "APPS  https://<app>.homeserver.invalid → Traefik 192.0.2.40 · allowlist 127.0.0.1/32" in out
    assert " Nodes      ● homeserver-cp-1 Ready · ● homeserver-worker-1 Ready · etcd healthy" in out
    assert f" Flux       main@{COMMIT[:7]}, the same as the branch · 5 of 5 Kustomizations ready" in out
    assert " Kubernetes API   https://192.0.2.30:6443" in out
    assert "BACKUPS  no backup job: 300, 301 and 310 are not backed up" in out
    assert "etcd snapshot                —  none yet  in ~/.talos/homeserver/etcd  → make etcd-snapshot" in out
    assert out.rstrip().endswith("make status TYPE=apps|admin|vms|storage|backups|cluster|nas · JSON=1"), out
    assert max(len(line) for line in lines) <= 100, out


def test_make_status_exits_with_the_reports_code(tree, tmp_path):
    """make exits 0 when nothing is wrong, and otherwise 2, as for any failed recipe; the report's own code ends its
    last line and is the "Error N" make names."""
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = make_status(tree, state)
    assert rc == 0, out + err
    assert out.startswith("reading  ") and "exit 0 · nothing is wrong" in out, out

    # A storage that is not available is a problem: exit 1.
    seed(state, storage_total={"local-lvm": 200 * GIB, "local": 64 * GIB}, inactive=["local"])
    rc, out, err = make_status(tree, state)
    assert rc == 2 and "exit 1 · 1 problem" in out and make_failed(err, 1), out + err
    assert "the storage is unknown" in out

    # A host whose commands are not there could not be read: exit 2.
    seed(state)
    rc, out, err = make_status(tree, state, fake=False)
    assert rc == 2 and "exit 2 · 1 source not read" in out and make_failed(err, 2), out + err
    assert "could not be read: pvesh get /cluster/resources" in out

    # A section make status does not have: make refuses it before anything runs.
    rc, out, err = make_status(tree, state, "TYPE=vms,disks")
    assert rc == 2 and "make status takes TYPE=apps,admin,vms,storage,backups,cluster,nas, not disks" in err, out + err
    assert "ATTENTION" not in out and "reading" not in out

    # The playbook refuses it too, given by hand: exit 3, the run having failed, and no report.
    rc, out, err = make_status(tree, state, types="vms,disks")
    assert rc == 2 and make_failed(err, 3), out + err
    assert "make status has no section disks" in out + err and "the run failed" in err
    assert not (tree / ".cache" / "status" / "report.json").exists()


def test_an_app_that_fails_is_a_problem(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    crash = {"reason": "CrashLoopBackOff", "message": "back-off 5m0s", "restarts": 14, "finished": at(minutes=-2)}
    seed_cluster(tmp_path / "talos.json", pod_states={"jellyfin": crash})
    rc, out, err = make_status(tree, state, "TYPE=apps")
    assert rc == 2 and make_failed(err, 1), out + err
    problem = report_of(tree)["attention"][0]
    assert (problem["level"], problem["subject"]) == ("problem", "jellyfin")
    assert problem["text"] == "CrashLoopBackOff, 14 restarts, the last 2 m ago"
    kubeconfig = "~/.talos/homeserver/kubeconfig"
    assert problem["command"] == f"kubectl --kubeconfig {kubeconfig} -n media logs deploy/jellyfin --previous"
    assert " jellyfin       media       ● failing" in out, out


def test_json_prints_the_report_alone(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = make_status(tree, state, "JSON=1", "TYPE=storage")
    assert rc == 0, out + err
    report = json.loads(out)
    assert report["types"] == ["storage"] and report == report_of(tree)
    assert "vms" in report["proxmox"] and [s["id"] for s in report["proxmox"]["storages"]] == ["local", "local-lvm"]


def test_another_clusters_credentials_are_not_read_with(tree, tmp_path):
    """A kubeconfig naming another API server, as a test host's run would find the home cluster's, is reported and
    never used: no kubectl runs."""
    state = tmp_path / "pve.json"
    seed(state)
    credentials(talos_dir(tmp_path), server="https://192.0.2.99:6443")
    (tmp_path / "talos.json").write_text(json.dumps({"mutations": 0}))
    rc, out, err = make_status(tree, state)
    assert rc == 2 and make_failed(err, 2), out + err
    unread = report_of(tree)["attention"][0]
    assert unread["subject"] == "cluster" and "names the server https://192.0.2.99:6443, not" in unread["text"]
    assert "another cluster's credentials" in unread["text"]


def test_a_stopped_cluster_is_no_unread_source(tree, tmp_path):
    """With its VMs stopped, a cluster that does not answer is explained by them: their start is the thing to do."""
    state = tmp_path / "pve.json"
    seed(state)
    pve = json.loads(state.read_text())
    for vmid in ("300", "301"):
        pve["vms"][vmid]["status"] = "stopped"
    state.write_text(json.dumps(pve))
    seed_cluster(tmp_path / "talos.json", bootstrapped=False)
    rc, out, err = make_status(tree, state, status_timeout=5)
    report = report_of(tree)
    assert rc == 0 and report["exit"] == 0, out + err
    assert [(a["subject"], a["command"]) for a in report["attention"] if a["subject"] == "stopped"] == [
        ("stopped", "make resume ALL=1")
    ]
    assert "CLUSTER  not read: 300 homeserver-cp-1 and 301 homeserver-worker-1 are stopped" in out, out


def test_the_apps_are_asked_from_here(tree, tmp_path):
    """The probes run: every app's name is under .invalid, which never resolves, and Traefik's address is one that
    never answers, so each says so, quickly."""
    state = tmp_path / "pve.json"
    seed(state)
    start = time.monotonic()
    rc, out, err = make_status(tree, state, "TYPE=apps", status_probe_apps=True, status_probe_timeout=1)
    assert time.monotonic() - start < 60
    report = report_of(tree)
    assert rc == 2 and report["exit"] == 1, out + err
    assert report["dns"]["state"] == "missing" and report["dns"]["address"] == "192.0.2.40"
    assert all(u["error"] for a in report["apps"] if a["name"] != "homeassistant" for u in a["urls"])
    assert ("problem", "traefik") in [(a["level"], a["subject"]) for a in report["attention"]]
    assert "probes ok " in out.splitlines()[0], out
    assert "Until DNS has them, these lines in /etc/hosts reach the apps from here:" in out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_host_that_does_not_answer(tree, tmp_path):
    """A Proxmox host that refuses the connection is a report that says so, exit 2, not a failed run."""
    state = tmp_path / "pve.json"
    seed(state)
    # An inventory of its own: given with -e, the connection would apply to the report's own play on localhost too.
    (tree / "unreachable.ini").write_text(
        f"[proxmox]\nhomeserver ansible_connection=ssh ansible_host=127.0.0.1 ansible_port={free_port()}\n"
        "[nas]\ntruenas\n[kubernetes]\nhomeserver-k8s\n"
    )
    rc, out, err = make_status(tree, state, inventory="unreachable.ini", local=False)
    assert rc == 2 and make_failed(err, 2), out + err
    assert "proxmox did not answer" in out.splitlines()[0], out
    assert "the Proxmox host could not be read: " in out and "Connection refused" in out and "VMS" not in out
    assert report_of(tree)["exit"] == 2


# ------------------------------------------------------------------------------------------------ the NAS ---

# What roles/truenas's status entry point may ask: truenas_info's reads, nothing that changes the NAS.
NAS_READS = {
    "auth.login_with_api_key",
    "network.configuration.config",
    "disk.details",
    "system.info",
    "system.version",
    "system.ready",
    "interface.websocket_interface",
    "interface.websocket_local_ip",
    "interface.query",
    "interface.has_pending_changes",
    "pool.query",
    "pool.dataset.query",
    "service.query",
    "systemdataset.config",
    "sharing.nfs.query",
    "pool.scrub.query",
    "alert.list",
    "pool.snapshottask.query",
}


def seed_nas(path, **more):
    """The NAS as roles/truenas leaves it: both pools imported and scrubbed three days ago, the Nextcloud dataset
    shared and snapshotted hourly, the last an hour ago, and NFS running."""
    path.write_text(json.dumps(fake_truenas.initial_state("127.0.0.1")))
    fake = fake_truenas.FakeTrueNAS(path)
    fake.load()
    for guid in ("1001", "1002"):
        fake._import_pool({"guid": guid})
    for pool in fake.s["pools"]:
        pool["scan"] = fake_truenas.finished_scrub(end=time.time() - 3 * 86400)
    fake.s["datasets"]["persistent/nextcloud"] = fake.new_dataset("persistent/nextcloud", {})
    fake.m_sharing_nfs_create({"path": "/mnt/persistent/nextcloud", "hosts": ["192.0.2.31"]})
    fake.m_pool_snapshottask_create(
        {"dataset": "persistent/nextcloud", "schedule": {"hour": "*"}, "lifetime_value": 2, "lifetime_unit": "DAY"}
    )
    fake.s["snapshot_tasks"][-1]["state"] = {"state": "FINISHED", "datetime": fake_truenas.date(time.time() - 3700)}
    next(s for s in fake.s["services"] if s["service"] == "nfs").update(enable=True, state="RUNNING")
    fake.s.update(more)
    fake.s["mutations"] = 0
    fake.save()


@pytest.fixture(scope="module")
def nas(tmp_path_factory):
    """fake_truenas.py on a port of its own, with a certificate of its own, as the idempotence tests run it."""
    work = tmp_path_factory.mktemp("nas")
    cert, key, state, calls = work / "cert.pem", work / "key.pem", work / "state.json", work / "calls.jsonl"
    subj = ["-subj", "/CN=localhost", "-days", "1"]
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, *subj],
        check=True,
        capture_output=True,
    )
    fingerprint = subprocess.run(
        ["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    seed_nas(state)
    port = free_port()
    with (work / "fake.log").open("w") as log:
        proc = subprocess.Popen(
            [
                *[sys.executable, HERE / "fake_truenas.py", "--port", str(port), "--cert", cert, "--key", key],
                *["--state", state, "--call-log", calls],
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail("fake TrueNAS did not start")
        yield {"port": port, "state": state, "calls": calls, "fingerprint": fingerprint.split("=", 1)[1]}
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def nas_tree(tmp_path):
    """local.yml.example's setup, a NAS, a cluster and Home Assistant, with the cluster's credentials and state as
    the tree fixture has them."""
    root = example_tree(tmp_path / "tree")
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "Makefile", root)
    (root / "run").mkdir()
    (tmp_path / "home").mkdir()
    credentials(talos_dir(tmp_path))
    seed_cluster(tmp_path / "talos.json")
    return root


def nas_vars(nas, **more):
    """The fake NAS's address, port, certificate and key, in place of local.yml's and the vault's."""
    return {
        "truenas_api_hosts": ["127.0.0.1"],
        "truenas_api_port": nas["port"],
        "truenas_api_cert_sha256": nas["fingerprint"],
        "truenas_api_key": fake_truenas.API_KEY,
        **more,
    }


def nas_calls(nas):
    return [json.loads(line)["method"] for line in nas["calls"].read_text().splitlines()]


def test_the_nas_is_read_through_its_api(nas_tree, nas, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    alert = {
        "uuid": "1",
        "source": "SMART",
        "klass": "SMART",
        "args": {},
        "node": "A",
        "key": "sda",
        "datetime": fake_truenas.date(time.time() - 600),
        "last_occurrence": fake_truenas.date(time.time() - 600),
        "dismissed": False,
        "mail": None,
        "text": "Device: %(device)s, %(message)s",
        "id": "1",
        "level": "CRITICAL",
        "formatted": "Device: /dev/sda, Self-Test Log error count increased",
        "one_shot": False,
    }
    seed_nas(nas["state"], alerts=[alert, alert | {"uuid": "2", "level": "WARNING", "dismissed": True}])
    nas["calls"].write_text("")
    rc, out, err = make_status(nas_tree, state, **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == 2 and report["exit"] == 1, out + err
    assert "nas ok " in out.splitlines()[0], out
    found = report["nas"]
    assert [(p["name"], p["layout"], p["status"]) for p in found["pools"]] == [
        ("persistent", "raidz1", "ONLINE"),
        ("ephemeral", "stripe", "ONLINE"),
    ]
    assert found["nfs"] == {"enabled": True, "state": "RUNNING", "shares": 1}
    assert [(a["level"], a["subject"], a["text"]) for a in report["attention"] if a["subject"] == "nas"] == [
        ("problem", "nas", "TrueNAS alerts (critical): Device: /dev/sda, Self-Test Log error count increased")
    ]
    assert "NAS  truenas · TrueNAS 25.10.7 · up 34 d" in out, out
    assert " persistent  raidz1  online   ████░░░░░░░░  31%  2.2 / 7.1 TiB  scrub 3 d ago, 0 errors" in out, out
    assert " Snapshots  nextcloud hourly 1 h ago" in out and " NFS        running · 1 share" in out, out
    assert "TrueNAS https://192.0.2.10" in " ".join(out.split()), out
    # One login, nothing but reads, nothing changed, and no secret anywhere.
    calls = nas_calls(nas)
    assert calls.count("auth.login_with_api_key") == 1 and set(calls) <= NAS_READS, calls
    assert json.loads(nas["state"].read_text())["mutations"] == 0
    written = (nas_tree / ".cache" / "status" / "report.json").read_text()
    for secret in (fake_truenas.API_KEY, fake_truenas.SYSTEM_SERIAL, fake_truenas.PRIVATE_KEY):
        assert secret not in out + err + written, secret


def test_a_nas_on_another_release_is_a_thing_to_do(nas_tree, nas, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"], version="26.04.1")
    rc, out, err = make_status(nas_tree, state, "TYPE=nas", **nas_vars(nas))
    assert rc == 0, out + err
    todo = [a for a in report_of(nas_tree)["attention"] if a["subject"] == "nas"]
    assert [(a["level"], a["text"]) for a in todo] == [
        ("todo", "the NAS runs TrueNAS 26.04.1, and its configuration is written for 25.10")
    ]
    assert todo[0]["command"].startswith("make deploy TYPE=truenas refuses it until truenas_supported_version")


@pytest.mark.parametrize(
    ("given", "why"),
    [
        pytest.param({"truenas_api_port": None}, "Connection refused", id="no-answer"),
        pytest.param(
            {"truenas_api_cert_sha256": "00" * 32}, "not the pinned one; the API key was not sent", id="another-cert"
        ),
    ],
)
def test_a_nas_that_is_not_read(nas_tree, nas, tmp_path, given, why):
    """A NAS that does not answer, or presents another certificate than the pinned one, is a source not read, exit 2,
    and the key goes nowhere."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"])
    nas["calls"].write_text("")
    given = {k: (free_port() if v is None else v) for k, v in given.items()}
    rc, out, err = make_status(nas_tree, state, **nas_vars(nas, **given))
    report = report_of(nas_tree)
    assert rc == 2 and report["exit"] == 2, out + err
    assert "nas not read " in out.splitlines()[0], out
    unread = next(a for a in report["attention"] if a["subject"] == "nas")
    assert unread["level"] == "unread" and unread["text"].startswith("the NAS could not be read: ")
    assert why in unread["text"], unread
    assert "NAS  not read" in out and "auth.login_with_api_key" not in nas_calls(nas)


# ------------------------------------------------------------------------------------------------ backups ---
DAY = 86400


def seed_backups(path, runs, vmids=(300, 301, 310), job=True, next_in=4 * 3600):
    """The NAS's backup storage on the fake host, the nightly job over `vmids` as roles/proxmox_backup makes it, due
    next in `next_in` seconds, and `runs` seeded through fake_pve.backup_run: each (days before the last run due,
    {vmid: why it failed})."""
    state = json.loads(path.read_text())
    state["storage"].append(
        {
            "storage": "homeserver-backup",
            "type": "nfs",
            "content": "backup",
            "server": "192.0.2.10",
            "export": "/mnt/persistent/proxmox/backup",
            "path": "/mnt/pve/homeserver-backup",
            "options": "vers=4.2",
        }
    )
    next_run = int(time.time()) + next_in
    if job:
        state["backup_jobs"] = {
            "vm-nightly": {
                "id": "vm-nightly",
                "schedule": "02:00",
                "storage": "homeserver-backup",
                "vmid": ",".join(map(str, vmids)),
                "mode": "snapshot",
                "compress": "zstd",
                "prune-backups": "keep-daily=7,keep-weekly=4",
                "enabled": "1",
            }
        }
        state["next_runs"] = {"vm-nightly": next_run}
    for days, failures in runs:
        fake_pve.backup_run(state, next_run - (days + 1) * DAY + 1, list(vmids), "homeserver-backup", failures)
    path.write_text(json.dumps(state))
    return state


FREEZE = "VM 301 qmp command 'guest-fsfreeze-freeze' failed - got timeout"


def test_the_backups_of_this_setups_vms(nas_tree, nas, tmp_path):
    """The last run failed for the worker, and Home Assistant was added to the job after it ran: the failure with its
    reason from the run's log, the VM with no backup yet, and the etcd snapshots beside them."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_backups(state, [(1, {}), (0, {301: FREEZE})], vmids=(300, 301))
    pve = json.loads(state.read_text())
    pve["backup_jobs"]["vm-nightly"]["vmid"] = "300,301,310"
    state.write_text(json.dumps(pve))
    snapshots = talos_dir(tmp_path) / "etcd"
    snapshots.mkdir()
    taken = datetime.now(UTC) - timedelta(days=3, hours=1)
    (snapshots / f"etcd-{taken:%Y%m%dT%H%M%SZ}.db").write_text("")
    seed_nas(nas["state"])
    rc, out, err = make_status(nas_tree, state, "TYPE=backups", **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == 2 and report["exit"] == 1, out + err
    # The ages are the wall clock's: the run started 20 h before the report, give or take the run's own seconds.
    attention = [(a["level"], re.sub(r"\d\d h (ago|old)", r"NN h \1", a["text"])) for a in report["attention"]]
    assert attention == [
        ("problem", f"the backup of 301 homeserver-worker-1 failed NN h ago: {FREEZE}; the last good one is NN h old"),
        ("todo", "310 homeassistant has no backup yet: vm-nightly runs next in 3 h 59 m"),
    ], report["attention"]
    assert json.loads(state.read_text())["mutations"] == pve["mutations"]
    lines = [re.sub(r"\d\d h ago", "NN h ago", line) for line in out.splitlines()]
    # The hosts are read at once, so the line names them in the order they finished.
    assert lines[0].startswith("reading  ") and "proxmox ok " in lines[0], out
    assert max(len(line) for line in lines) <= 100, out
    assert "BACKUPS  vm-nightly → homeserver-backup · 02:00 · keeps 7 daily, 4 weekly · next in 3 h 59 m" in lines
    assert "  300  homeserver-cp-1       NN h ago  ok        7.0 GiB · 2 kept" in lines, out
    failed = "  301  homeserver-worker-1   NN h ago  failed    VM 301 qmp command 'guest-fsfreeze-freeze' failed -"
    assert failed in lines, out
    assert " " * 29 + "NN h ago  ok        1.0 GiB · 1 kept" in lines, out
    assert "  310  homeassistant                —  none yet" in lines, out
    assert "       etcd snapshot          3 d ago            1 in ~/.talos/homeserver/etcd" in lines, out
    assert "VMS  " not in out and "NAS  " not in out


# What each setup reports on a host as roles leave it: its sections, in order, what needs attention and the exit code.
# The Proxmox host waits for a restart and an upgrade in every one (seed()); the rest is the setup's own.
SETUPS = {
    "proxmox": (
        PROXMOX_ONLY_LOCAL_YML,
        ["ADMIN", "VMS", "STORAGE"],
        [("todo", "upgrades"), ("todo", "local-lvm")],
        0,
    ),
    "proxmox-cluster": (
        PROXMOX_ONLY_CLUSTER_LOCAL_YML,
        ["APPS", "ADMIN", "VMS", "STORAGE", "BACKUPS", "CLUSTER"],
        [
            ("todo", "suspended"),
            ("todo", "allowlist"),
            ("todo", "upgrades"),
            ("todo", "restart"),
            ("todo", "local-lvm"),
            ("todo", "backup"),
        ],
        0,
    ),
    "nas": (
        example_without_kubernetes(),
        ["ADMIN", "VMS", "STORAGE", "NAS"],
        [("todo", "upgrades"), ("todo", "local-lvm")],
        0,
    ),
    "nas-cluster": (
        None,
        ["APPS", "ADMIN", "VMS", "STORAGE", "BACKUPS", "CLUSTER", "NAS"],
        [
            ("todo", "suspended"),
            ("todo", "allowlist"),
            ("todo", "upgrades"),
            ("todo", "restart"),
            ("todo", "local-lvm"),
        ],
        0,
    ),
}


@pytest.mark.parametrize("setup", SETUPS)
def test_each_setup_reports_what_it_has(setup, nas, tmp_path):
    local_yml, sections, attention, code = SETUPS[setup]
    root = example_tree(tmp_path / "tree", local_yml)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "Makefile", root)
    (root / "run").mkdir()
    (tmp_path / "home").mkdir()
    credentials(talos_dir(tmp_path))
    seed_cluster(tmp_path / "talos.json")
    state = tmp_path / "pve.json"
    seed(state)
    if setup == "nas-cluster":
        seed_backups(state, [(0, {})])
    seed_nas(nas["state"])
    given = nas_vars(nas) if setup.startswith("nas") else {}
    rc, out, err = make_status(root, state, cluster=setup.endswith("cluster"), **given)
    assert (root / ".cache" / "status" / "report.json").exists(), out + err
    report = report_of(root)
    headers = [m.group(1) for m in map(re.compile(r"([A-Z]{3,})(  |$)").match, out.splitlines()) if m]
    assert headers[1:] == sections, out
    assert [(a["level"], a["subject"]) for a in report["attention"]] == attention, out
    assert (rc, report["exit"]) == (2 if code else 0, code), out + err
    assert max(len(line) for line in out.splitlines()) <= 100, out
