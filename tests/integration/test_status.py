"""Runs just status, the real status.yml with its callback and the recipe's exit codes, against the fake Proxmox host,
the fake cluster and the fake NAS.

The playbook, roles, group_vars and justfile are copied into a scratch tree with a local.yml of a Proxmox-only setup
with a cluster and Home Assistant, as the policy tests do. The Proxmox host is reached locally with the fake's commands
first on PATH, and the cluster through fake_talos.py's kubectl, talosctl and git, with credentials of its own in a
directory under the test's own HOME, so no run ever reads the workstation's. Checked: the report's file and text, the
callback's output (one line on what was read, then the report, and no task banners), that nothing on the host or in the
cluster changed, the apps and their states, the nodes' memory and disks from kubelet in place of Proxmox's view, the
exit codes (the report's own, which just exits with: 0 when nothing is wrong, 1 for a problem, 2 for a source that could
not be read, 3 for a refused run, also on the report's last line), --json, a section the recipe does not have, another
cluster's kubeconfig, and a host that does not answer at all. With a NAS (local.yml.example's setup), the NAS is read
through roles/truenas's status entry point against fake_truenas.py: one login, no change, no secret in the report, the
Macs' Time Machine backups judged by local.yml's Macs from one snapshot read for every enabled share, each Mac's from
its own dataset, and a NAS that does not answer or presents another certificate is a source not read. The apps' probes
ask addresses that never answer here, under a domain that never resolves (.invalid), so what they find does not depend
on the network.

Run: just test idempotence
"""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jinja2
import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests" / "policy"))
sys.path.insert(0, str(ROOT / "tests"))
import fake_pve  # noqa: E402
import fake_tailscale  # noqa: E402
import fake_talos  # noqa: E402
import fake_truenas  # noqa: E402
from helpers import (  # noqa: E402
    CLUSTER_VARS,
    PROXMOX_ONLY_CLUSTER_LOCAL_YML,
    PROXMOX_ONLY_LOCAL_YML,
    example_tree,
    example_without_kubernetes,
)
from vt import VT  # noqa: E402

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
    """The cluster as Flux leaves it: Homepage, Jellyfin, Sonarr (suspended by just suspend two days ago), Nextcloud
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
    """The repository's playbooks, roles, group_vars, callback and justfile, with a Proxmox-only cluster's local.yml,
    the cluster's credentials in a directory of their own, and the fake cluster's state beside the fake host's."""
    root = example_tree(tmp_path / "tree", PROXMOX_ONLY_CLUSTER_LOCAL_YML)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "justfile", root)
    (root / "run").mkdir()
    (tmp_path / "home").mkdir()
    credentials(talos_dir(tmp_path))
    seed_cluster(tmp_path / "talos.json")
    return root


# What just reads from the environment, which the just that runs the tests (just test idempotence) would hand on: its
# own settings, and the virtualenv it chose, which each run here names itself.
JUST_VARIABLES = ("JUST_", "VENV", "VIRTUAL_ENV")


def environment(tree, state, fake=True, **more):
    """The fakes first on PATH (without fake_pve's, when not `fake`: a host without Proxmox's commands), each with
    its state, and HOME the test's own, so that nothing reads the workstation's credentials. SSH shares no
    connection, whose socket's path under that HOME would be too long. Nothing of the just that runs the tests."""
    fakes = [str(HERE / "fake_talos_bin"), *([str(HERE / "fake_pve_bin")] if fake else [])]
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "STATUS_", "NO_COLOR", *JUST_VARIABLES))
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


def just_status(tree, state, *args, fake=True, inventory="inventory.example", local=True, cluster=True, **more):
    """just status in the tree with `args` (sections, --json), this Python's virtualenv, and this test's inventory and
    variables after them, which the recipe hands ansible-playbook."""
    ansible_args = ["-i", inventory, "-e", extra_vars(tree, local, cluster, **more)]
    cmd = ["just", "status", *args, "--", *ansible_args]
    env = environment(tree, state, fake=fake) | {"VENV": sys.prefix}
    env.pop("ANSIBLE_STDOUT_CALLBACK")
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def report_of(tree):
    return json.loads((tree / ".cache" / "status" / "report.json").read_text())


def reading(out):
    """The callback's line on what was read, which goes above the report's exit line."""
    lines = out.rstrip("\n").splitlines()
    assert len(lines) >= 2 and lines[-2].startswith("reading  "), out
    return lines[-2]


def just_status_on_a_terminal(tree, state, *args, rows=20, columns=120, **more):
    """just status as just_status runs it, but on a terminal of its own, `rows` high, without colours: what the run
    wrote to it, every control sequence included."""
    master, terminal = pty.openpty()
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    ansible_args = ["-i", "inventory.example", "-e", extra_vars(tree, **more)]
    env = environment(tree, state) | {"VENV": sys.prefix, "TERM": "xterm-256color", "NO_COLOR": "1"}
    env.pop("ANSIBLE_STDOUT_CALLBACK")
    proc = subprocess.Popen(
        ["just", "status", *args, "--", *ansible_args],
        cwd=tree,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=terminal,
        stderr=terminal,
    )
    os.close(terminal)
    chunks = []
    while select.select([master], [], [], 120)[0]:
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO: every process that wrote to the terminal has ended
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    return proc.wait(timeout=60), b"".join(chunks).decode()


def without_times(text):
    """A report's text without what differs between two runs: when it was read, and how long each host took."""
    text = re.sub(r"\d\d:\d\d", "HH:MM", text)
    return re.sub(r"\d+\.\d s", "N s", text)


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
    # In the sections' order: the VMs', the storage's, the apps', the backups'.
    assert [(a["subject"], a["command"]) for a in report["attention"]] == [
        ("reboot", "reboot it at a quiet time"),
        ("upgrades", "just deploy proxmox"),
        ("restart", "qm reboot 301"),
        # The disks provisioned on local-lvm add up to more than it holds, as on a host run with overcommit allowed.
        ("local-lvm", "keep its use well below 100% (pvesm status)"),
        ("suspended", "just resume --only sonarr"),
        (
            "allowlist",
            "the first visits (scripts/runbooks/first-run.md), then kubernetes_ingress_allowed_range in local.yml "
            "and just deploy kubernetes",
        ),
        # Without a NAS and without vm_backup_storage there is no backup job.
        (
            "backup",
            "vm_backup_storage in local.yml names where they go (a USB disk, or a Proxmox Backup Server), then just "
            "deploy proxmox",
        ),
    ]
    assert report["attention"][4]["text"] == f"sonarr is suspended since {at(days=-2)[:10]}"
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
    # Every other VM on the host, as Proxmox sees it, the running one with its day's history.
    assert report["proxmox"]["others"] == [
        {"vmid": 9000, "name": "ubuntu-template", "status": "template"},
        {
            "vmid": 9001,
            "name": "another-project",
            "status": "running",
            "uptime": 3600,
            "cpu": 0.1,
            "cores": 2,
            "memory": {"used": 2 * GIB, "total": 4 * GIB},
            "cpu_24h": [0.1] * 12,
            "boot_disk": 0,
        },
    ]
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

    # The callback: the report, with what was read above its exit line, and nothing of Ansible's own.
    lines = out.splitlines()
    assert lines[0].startswith("homeserver  ") and lines[-1].startswith("exit 0 · "), out
    line = reading(out)
    assert "proxmox ok " in line and "cluster ok " in line, out
    # The NAS's host reads nothing without a NAS, so the line leaves it out.
    assert " nas " not in line and "NAS  " not in out, out
    assert "TASK [" not in out and "PLAY RECAP" not in out and "\033[" not in out, out
    assert "VMS  pve · Proxmox VE 9.2.2-1 · kernel 7.0.2-6-pve" in out
    assert " 9000  ◇ ubuntu-template      template" in out
    assert " 9001  ◆ another-project      1 h 0 m   10%  ▁▁▁▁▁▁▁▁▁▁▁▁   2.0 / 4.0   —" in out
    assert " ◆ ◇ not this repo's: another project's, or made by hand; shown, never a problem or a thing to do" in out
    assert "disks 216 of 200 GiB budgeted" in out
    assert "APPS  https://<app>.homeserver.invalid → Traefik 192.0.2.40 · allowlist 127.0.0.1/32" in out
    assert " Nodes      ● homeserver-cp-1 Ready · ● homeserver-worker-1 Ready · etcd healthy" in out
    assert f" Flux       main@{COMMIT[:7]}, the same as the branch · 5 of 5 Kustomizations ready" in out
    assert " Kubernetes API   https://192.0.2.30:6443" in out
    assert "BACKUPS  no backup job: 300, 301 and 310 are not backed up" in out
    assert "etcd snapshot                —  none yet  in ~/.talos/homeserver/etcd  → just etcd-snapshot" in out
    assert out.rstrip().endswith(" · sections: admin vms nas storage cluster apps tailscale backups · --json"), out
    assert max(len(line) for line in lines) <= 100, out


def test_a_node_named_in_advance_reads_the_same(tree, tmp_path):
    """With status_proxmox_node given, the host's name is not asked: with the VMs shown the guest list is read alone
    first, and without them (just status storage) there is no first read, the guest list read with the rest. Each
    report is the one the run that asks the name makes."""
    state = tmp_path / "pve.json"
    seed(state)
    for args in ((), ("storage",)):
        rc, asked, err = just_status(tree, state, *args)
        assert rc == 0, asked + err
        rc, named, err = just_status(tree, state, *args, status_proxmox_node="pve")
        assert rc == 0, named + err
        assert without_times(named) == without_times(asked)
    assert "◆ another-project" not in named and "◆ another-project" in just_status(tree, state)[1]


def test_just_status_exits_with_the_reports_code(tree, tmp_path):
    """just status exits with the report's own code, which also ends its last line, and adds no line of its own."""
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = just_status(tree, state)
    assert rc == 0, out + err
    assert out.startswith("homeserver  ") and reading(out) and "exit 0 · nothing is wrong" in out, out

    # A storage that is not available is a problem: exit 1.
    seed(state, storage_total={"local-lvm": 200 * GIB, "local": 64 * GIB}, inactive=["local"])
    rc, out, err = just_status(tree, state)
    assert rc == 1 and "exit 1 · 1 problem" in out and "error:" not in err, out + err
    assert "the storage is unknown" in out

    # A host whose commands are not there could not be read: exit 2.
    seed(state)
    rc, out, err = just_status(tree, state, fake=False)
    assert rc == 2 and "exit 2 · 1 source not read" in out, out + err
    assert "could not be read: pvesh get /cluster/resources" in out

    # A section just status does not have: the recipe refuses it before anything runs, with 3.
    rc, out, err = just_status(tree, state, "vms", "disks")
    assert rc == 3 and "just status has no section disks: it takes admin vms nas storage cluster" in err, out + err
    assert "ATTENTION" not in out and "reading" not in out
    # And a make-style parameter, rather than reading it as a section.
    rc, out, err = just_status(tree, state, "TYPE=vms")
    assert rc == 3 and "TYPE=vms: just takes no make-style parameter" in err, out + err

    # The playbook refuses it too, given by hand: exit 3, the run having failed, and no report.
    rc, out, err = just_status(tree, state, types="vms,disks")
    assert rc == 3, out + err
    assert "The report has no section disks" in out + err and "the run failed" in err
    assert not (tree / ".cache" / "status" / "report.json").exists()


def test_an_app_that_fails_is_a_problem(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    crash = {"reason": "CrashLoopBackOff", "message": "back-off 5m0s", "restarts": 14, "finished": at(minutes=-2)}
    seed_cluster(tmp_path / "talos.json", pod_states={"jellyfin": crash})
    rc, out, err = just_status(tree, state, "apps")
    assert rc == 1, out + err
    problem = report_of(tree)["attention"][0]
    assert (problem["level"], problem["subject"]) == ("problem", "jellyfin")
    assert problem["text"] == "CrashLoopBackOff, 14 restarts, the last 2 m ago"
    kubeconfig = "~/.talos/homeserver/kubeconfig"
    assert problem["command"] == f"kubectl --kubeconfig {kubeconfig} -n media logs deploy/jellyfin --previous"
    assert " jellyfin       media       ● failing" in out, out


def test_json_prints_the_report_alone(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = just_status(tree, state, "storage", "--json")
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
    rc, out, err = just_status(tree, state)
    assert rc == 2, out + err
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
    rc, out, err = just_status(tree, state, status_timeout=5)
    report = report_of(tree)
    assert rc == 0 and report["exit"] == 0, out + err
    assert [(a["subject"], a["command"]) for a in report["attention"] if a["subject"] == "stopped"] == [
        ("stopped", "just resume --all")
    ]
    assert "CLUSTER  not read: 300 homeserver-cp-1 and 301 homeserver-worker-1 are stopped" in out, out


def test_the_apps_are_asked_from_here(tree, tmp_path):
    """The probes run: every app's name is under .invalid, which never resolves, and Traefik's address is one that
    never answers, so each says so, quickly."""
    state = tmp_path / "pve.json"
    seed(state)
    start = time.monotonic()
    rc, out, err = just_status(tree, state, "apps", status_probe_apps=True, status_probe_timeout=1)
    assert time.monotonic() - start < 60
    report = report_of(tree)
    assert rc == report["exit"] == 1, out + err
    assert report["dns"]["state"] == "missing" and report["dns"]["address"] == "192.0.2.40"
    assert all(u["error"] for a in report["apps"] if a["name"] != "homeassistant" for u in a["urls"])
    assert ("problem", "traefik") in [(a["level"], a["subject"]) for a in report["attention"]]
    # Each host's play asked its own apps: the cluster's the apps, the Proxmox host's Home Assistant, at an address
    # that never answers either.
    apps = {a["name"]: a for a in report["apps"]}
    assert apps["homeassistant"]["urls"][0]["error"], apps["homeassistant"]
    assert "Until DNS has them, these lines in /etc/hosts reach the apps from here:" in out


SPINNER = re.compile(r"reading  .*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] \d+\.\d s")


def test_on_a_terminal_each_section_shows_as_it_is_read(tree, tmp_path):
    """On a terminal, the report's head comes at once, then each section in its order as the hosts it needs are read,
    the sections still to come and the line on what is being read redrawn in place below them, a spinner beside what
    is still waited for, then cleared: what the terminal holds after the run is the report as a pipe gets it."""
    state = tmp_path / "pve.json"
    seed(state)
    rc, plain, err = just_status(tree, state)
    assert rc == 0, plain + err
    rc, stream = just_status_on_a_terminal(tree, state)
    assert rc == 0, stream
    assert SPINNER.search(stream), stream
    assert re.search(r" [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] APPS +waiting for the Proxmox host", stream), stream
    assert stream.index("homeserver  ") < stream.index("reading  "), stream
    terminal = VT(120, 20)
    terminal.feed(stream.encode())
    # Redrawn in place, each frame whole on its lines, and nothing written a terminal would not know.
    assert terminal.ups and terminal.frame_wraps == 0 and not terminal.unknown, (terminal.frame_wraps, terminal.unknown)
    screen = "\n".join(terminal.transcript()) + "\n"
    assert without_times(screen) == without_times(plain), screen
    # --json is no terminal's: the report alone, and nothing drawn.
    rc, stream = just_status_on_a_terminal(tree, state, "--json")
    assert rc == 0 and "\x1b[" not in stream and json.loads(stream) == report_of(tree), stream


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
    rc, out, err = just_status(tree, state, inventory="unreachable.ini", local=False)
    assert rc == 2, out + err
    assert "proxmox did not answer" in reading(out), out
    assert "the Proxmox host could not be read: " in out and "Connection refused" in out and "VMS" not in out
    assert report_of(tree)["exit"] == 2


# ------------------------------------------------------------------------------------------------ the NAS ---

# What roles/truenas's status entry point may ask: truenas_info's reads, nothing that changes the NAS.
ROUTE = "192.0.2.0/24"
TAILSCALE_SETTINGS = {"accept-routes": False, "accept-dns": False, "ssh": False, "auto-update": False}


def seed_tailscale(path, routes=(ROUTE,), approved=True, split_dns=None, **more):
    """The fake host's Tailscale as roles/proxmox_tailscale leaves it: installed, tailscaled and the guard running, the
    node logged in with its tag and preferences, the routes advertised (approved when `approved`), the guard's table
    as the role's own template writes it, and forwarding on with routes."""
    state = fake_tailscale.fresh_state()
    state["packages"]["installed"].update(fake_tailscale.REPOSITORY)
    for unit in ("tailscaled.service", fake_tailscale.GUARD):
        state["units"][unit] = {"enabled": True, "active": True}
    prefs = dict(fake_tailscale.DEFAULT_PREFS) | TAILSCALE_SETTINGS | {"advertise-routes": ",".join(routes)}
    state["node"] = {"backend": "Running", "tags": ["tag:homeserver"], "prefs": prefs}
    state["auto_approve"] = approved
    state["sysctl"]["net.ipv4.ip_forward"] = "1" if routes else "0"
    template = (ROOT / "roles" / "proxmox_tailscale" / "templates" / "tailnet-guard.nft.j2").read_text()
    state["table"] = (
        jinja2.Environment(trim_blocks=True)
        .from_string(template)
        .render(
            proxmox_tailscale_advertise_routes=list(routes),
            proxmox_tailscale_guard_hosts=[],
            proxmox_tailscale_guard_allow=["tcp/443"],
        )
    )
    state["split_dns"] = split_dns or {}
    state.update(more)
    path.write_text(json.dumps(state))
    return state


def tailscale_environment(state):
    """The Tailscale fake after fake_pve's on PATH, whose apt-get and systemctl the host's other reads use."""
    return {
        "FAKE_TAILSCALE_STATE": str(state),
        "FAKE_TAILSCALE_PYTHON": sys.executable,
        "FAKE_TAILSCALE_GUARD_FILE": str(state.parent / "tailnet-guard.nft"),
        "FAKE_TAILSCALE_SOURCES_DIR": str(state.parent),
        "FAKE_TAILSCALE_SYSTEMD_DIR": str(state.parent),
    }


@pytest.mark.parametrize("approved", [True, False], ids=["served", "not-approved"])
def test_the_hosts_tailscale(tree, tmp_path, approved):
    state = tmp_path / "pve.json"
    seed(state)
    tailscale = tmp_path / "tailscale.json"
    seed_tailscale(tailscale, approved=approved)
    env = environment(tree, state, **tailscale_environment(tailscale))
    env["PATH"] = env["PATH"].replace(
        str(HERE / "fake_pve_bin"), f"{HERE / 'fake_pve_bin'}:{HERE / 'fake_tailscale_bin'}"
    )
    variables = extra_vars(tree, tailscale_advertise_routes=[ROUTE])
    cmd = [str(BIN / "ansible-playbook"), "-i", "inventory.example", "status.yml", "--check", "-e", variables]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr

    # Reads only: on the host, all five at once, then this machine's own Tailscale for the tailnet's split DNS. (On
    # Linux, Python's uuid.getnode(), which Ansible calls for its ids, runs `ip link` too, which the fake's ip on PATH
    # records.)
    after = json.loads(tailscale.read_text())
    assert after["mutations"] == 0
    calls = [c for c in after["calls"] if c[0] != "ip"]
    assert sorted(calls[:5]) == [
        ["nft", "list", "table", "inet", "tailnet_guard"],
        ["sysctl", "-n", "net.ipv4.ip_forward"],
        ["tailscale", "get", "--json"],
        ["tailscale", "serve", "status", "--json"],
        ["tailscale", "status", "--json"],
    ]
    assert calls[5:] == [["tailscale", "dns", "status", "--json"]]

    report = report_of(tree)
    tail = report["tailscale"]
    name = f"pve.{fake_tailscale.SUFFIX}"
    assert (tail["backend"], tail["dns_name"], tail["tags"]) == ("Running", name, ["tag:homeserver"])
    assert tail["routes"] == [{"route": ROUTE, "wanted": True, "advertised": True, "served": approved}]
    assert tail["guard"] == {"loaded": True, "routes": [ROUTE], "guarded": [], "allow": {}}
    assert tail["forwarding"] is True and tail["differ"] == []
    # Nothing of another device, its owner or the tailnet's name, which for a personal tailnet is a login.
    text = json.dumps(report) + out
    for secret in [fake_tailscale.TAILNET_NAME, "laptop", *fake_tailscale.PEER_ADDRESSES]:
        assert secret not in text

    attention = [(a["level"], a["text"]) for a in report["attention"] if a["subject"] == "tailscale"]
    unapproved = f"Advertised but not approved, or served by another subnet router: {ROUTE}."
    expected = [*([] if approved else [unapproved]), "the tailnet has no split DNS for homeserver.invalid:"]
    assert [level for level, _ in attention] == ["todo"] * len(expected), attention
    assert all(text.startswith(e) for (_, text), e in zip(attention, expected, strict=True)), attention
    split_dns = next(a for a in report["attention"] if a["text"].startswith("the tailnet has no split DNS"))
    assert split_dns["command"] == "Tailscale's admin console, DNS (scripts/runbooks/tailscale.md, section 2)"

    assert "TAILSCALE  subnet router on the Proxmox host · 1.104.1 · ● running" in out, out
    assert f" Node       {name} · {fake_tailscale.NODE_ADDRESSES[0]} · tag:homeserver · key expiry off" in out
    served = "served by this host" if approved else "not served by this host"
    assert f" Routes     {ROUTE} advertised, {served}" in out
    assert " Guard      loaded · forwarding on" in out
    assert " Prefs      accept-routes off · accept-dns off · ssh off · auto-update off · as configured" in out
    assert re.search(rf" Proxmox VE +https://192\.0\.2\.20:8006 +https://{re.escape(name)}:8006", out), out
    tailnet = "through the route" if approved else "route not served by this host"
    assert re.search(rf" Kubernetes API +https://192\.0\.2\.30:6443 +{tailnet}$", out, re.M), out
    if approved:
        # Tailnet clients arrive as the host's own address, which Traefik's allowed range (loopback) leaves out.
        reach = f" Tailnet    the same URLs, through {ROUTE}\n"
        reach += "            clients arrive as 192.0.2.20, outside the allowlist\n"
        assert reach in out, out
    else:
        assert f" Tailnet    through {ROUTE}, which this host does not serve" in out, out
    assert max(len(line) for line in out.splitlines()) <= 100, out


def test_tailscale_that_is_not_running_is_a_problem(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    tailscale = tmp_path / "tailscale.json"
    seed_tailscale(tailscale)
    stopped = json.loads(tailscale.read_text())
    stopped["units"]["tailscaled.service"]["active"] = False
    stopped["table"] = None
    tailscale.write_text(json.dumps(stopped))
    env = environment(tree, state, **tailscale_environment(tailscale))
    env["PATH"] = env["PATH"].replace(
        str(HERE / "fake_pve_bin"), f"{HERE / 'fake_pve_bin'}:{HERE / 'fake_tailscale_bin'}"
    )
    variables = extra_vars(tree, tailscale_advertise_routes=[ROUTE])
    cmd = [str(BIN / "ansible-playbook"), "-i", "inventory.example", "status.yml", "--check", "-e", variables]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = report_of(tree)
    assert report["exit"] == 1
    item = next(a for a in report["attention"] if a["subject"] == "tailscale")
    assert item["level"] == "problem" and item["command"] == "just deploy proxmox"
    assert item["text"].startswith(
        "tailscaled does not answer on the Proxmox host: failed to connect to local tailscaled"
    )
    assert "TAILSCALE  subnet router on the Proxmox host · ● not running" in proc.stdout


# What just status may call on the NAS, reads alone: roles/truenas's status entry point, truenas_info with its status
# option, whose one pool.snapshot.query reads the enabled Time Machine shares' snapshots together, by name alone, and
# whose last three reads, the disks' temperatures and the NFS clients, may each fail without the NAS going unread.
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
    "sharing.smb.query",
    "pool.scrub.query",
    "alert.list",
    "pool.snapshottask.query",
    "pool.snapshot.query",
    "disk.temperatures",
    "nfs.get_nfs3_clients",
    "nfs.get_nfs4_clients",
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
        # Three days ago, and some minutes: the report's ages count from when the reading began.
        pool["scan"] = fake_truenas.finished_scrub(end=time.time() - 3 * 86400 - 600)
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


# The Macs local.yml lists for seed_macs' shares: laptop and desk back up, old is retired.
MACS = [
    {"name": "laptop", "quota_gib": 2048, "uid": 3101},
    {"name": "desk", "quota_gib": 1024, "uid": 3102},
    {"name": "old", "quota_gib": 2048, "uid": 3103, "retired": True},
]


def seed_macs(path, laptop_ago=7200, desk_ago=3600):
    """MACS' Time Machine shares as roles/truenas leaves them, SMB running: laptop's backup 400 GiB of a 2 TiB quota,
    its snapshots 20 GiB, its newest backup `laptop_ago` seconds old; desk's 100 GiB of 1 TiB, its snapshots 5 GiB, its
    newest `desk_ago` seconds old; and old's share disabled, a retired Mac's."""
    fake = fake_truenas.FakeTrueNAS(path)
    fake.load()
    fake.s["network"]["service_announcement"] = {"netbios": False, "mdns": True, "wsd": False}
    fake.s["smb"]["aapl_extensions"] = True
    fake.s["datasets"]["persistent/timemachine"] = fake.new_dataset("persistent/timemachine", {})
    for mac in MACS:
        dataset = f"persistent/timemachine/{mac['name']}"
        fake.s["datasets"][dataset] = fake.new_dataset(dataset, {"quota": str(mac["quota_gib"] * GIB)})
        fake.m_sharing_smb_create(
            {
                "name": f"tm-{mac['name']}",
                "path": f"/mnt/{dataset}",
                "purpose": "TIMEMACHINE_SHARE",
                "enabled": not mac.get("retired", False),
                "options": {"auto_snapshot": True},
            }
        )
    fake.s["space"]["persistent/timemachine/laptop"] = [400 * GIB, 1648 * GIB]
    fake.s["space"]["persistent/timemachine/desk"] = [100 * GIB, 924 * GIB]
    fake.s["snapshot_space"] = {"persistent/timemachine/laptop": 20 * GIB, "persistent/timemachine/desk": 5 * GIB}
    now = int(time.time())
    fake.s["snapshots"] = [
        f"persistent/timemachine/laptop@aapltm-{now - laptop_ago}",
        f"persistent/timemachine/laptop@aapltm-{now - laptop_ago - 86400}",
        "persistent/timemachine/laptop@weekly-2026-11-15_00-00",
        f"persistent/timemachine/desk@aapltm-{now - desk_ago}",
        f"persistent/timemachine/old@aapltm-{now - 90 * 86400}",
    ]
    next(s for s in fake.s["services"] if s["service"] == "cifs").update(enable=True, state="RUNNING")
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
    shutil.copy(ROOT / "justfile", root)
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
    rc, out, err = just_status(nas_tree, state, **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 1, out + err
    assert "nas ok " in reading(out), out
    found = report["nas"]
    assert [(p["name"], p["layout"], p["status"]) for p in found["pools"]] == [
        ("persistent", "raidz1", "ONLINE"),
        ("ephemeral", "stripe", "ONLINE"),
    ]
    assert found["nfs"] == {"enabled": True, "state": "RUNNING", "shares": 1, "clients": []}
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
    # No Time Machine share, so no snapshot is read.
    assert "pool.snapshot.query" not in calls, calls
    assert json.loads(nas["state"].read_text())["mutations"] == 0
    written = (nas_tree / ".cache" / "status" / "report.json").read_text()
    for secret in (fake_truenas.API_KEY, fake_truenas.SYSTEM_SERIAL, fake_truenas.PRIVATE_KEY):
        assert secret not in out + err + written, secret


def test_the_macs_backups_are_read_from_the_nas(nas_tree, nas, tmp_path):
    """Each Time Machine share's backup, judged by local.yml's Macs, from the share, its dataset and Samba's snapshots,
    in the same login and with nothing but reads: its use against its quota, its snapshots' share, and its newest
    backup, from one snapshot read for every enabled share, by name alone; a retired Mac's listed and never late. Then
    laptop's newest backup eleven days old while desk's is an hour old: laptop's own is late, a thing to do."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"])
    seed_macs(nas["state"])
    nas["calls"].write_text("")
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas, timemachine_backups=MACS))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 0, out + err
    found = report["nas"]
    assert found["smb"] == {"enabled": True, "state": "RUNNING", "shares": 2}
    keys = ("name", "configured", "retired", "enabled", "used", "quota", "snapshots_used")
    assert [tuple(b[key] for key in keys) for b in found["time_machine"]] == [
        ("tm-laptop", True, False, True, 400 * GIB, 2 * 2**40, 20 * GIB),
        ("tm-desk", True, False, True, 100 * GIB, 2**40, 5 * GIB),
        ("tm-old", True, True, False, 0, 2 * 2**40, 0),
    ]
    assert found["time_machine"][2]["last_backup"] == "", "a disabled share's time is not read"
    assert " SMB        running · 2 shares" in out, out
    macs = (
        " Macs       tm-laptop backed up 2 h ago, 0.4 / 2.0 TiB, snapshots 20 GiB\n"
        "            tm-desk backed up 1 h ago, 0.1 / 1.0 TiB, snapshots 5.0 GiB · tm-old retired\n"
    )
    assert macs in out, out
    calls = nas_calls(nas)
    assert calls.count("auth.login_with_api_key") == 1 and set(calls) <= NAS_READS, calls
    assert calls.count("sharing.smb.query") == 1, calls
    # The enabled shares' datasets together, by name alone: TrueNAS's fast path, which reads no property.
    logged = [json.loads(line) for line in nas["calls"].read_text().splitlines()]
    queries = [call["params"] for call in logged if call["method"] == "pool.snapshot.query"]
    datasets = ["persistent/timemachine/laptop", "persistent/timemachine/desk"]
    assert queries == [[[["dataset", "in", datasets]], {"select": ["name"]}]], queries
    assert json.loads(nas["state"].read_text())["mutations"] == 0

    seed_nas(nas["state"])
    seed_macs(nas["state"], laptop_ago=11 * 86400 + 600, desk_ago=3600)
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas, timemachine_backups=MACS))
    assert rc == 0, out + err
    attention = report_of(nas_tree)["attention"]
    todo = [(a["level"], a["subject"], a["text"]) for a in attention]
    assert todo == [("todo", "tm-laptop", "its newest Time Machine backup completed 11 d ago")], todo
    assert attention[0]["command"].startswith("on the Mac, Time Machine's settings"), attention
    assert " Macs       tm-laptop backed up 11 d ago, 0.4 / 2.0 TiB, snapshots 20 GiB" in out, out


# The NAS section's header in local.yml.example's setup, whether or not it also says the address the NAS answered at.
NAS_HEADER = r"NAS  truenas · TrueNAS 25\.10\.7( at [^·]+)? · up 34 d"
# What an NFSv4 client says of itself, and the files it holds open, which the report never carries.
NFS_CLIENT_NAME = "Linux NFSv4.2 FAKE-CLIENT-NAME-MUST-NOT-LEAK"
NFS_OPEN_FILE = "/FAKE-OPEN-FILE-MUST-NOT-LEAK"


def seed_details(path, refused=()):
    """The NAS seed_nas leaves, with something for each of the NAS section's details to say: two ports, both up, at
    2.5 Gb/s and 1 Gb/s; nvme3n1 hot, 74 °C of its critical 80.85, and nvme2n1 with three checksum errors, which make
    persistent not healthy; ssh running though the configuration keeps it stopped; the shares mounted by the Proxmox
    host over NFSv3 and by two NFSv4 clients behind its address, by the worker and an address of no machine of this
    setup over NFSv4, and an NFSv4 client that stopped renewing its lease, which no longer counts; the NAS's clock 95 s
    behind; and the methods `refused` refused."""
    fake = fake_truenas.FakeTrueNAS(path)
    fake.load()
    # The fake's own port, by whatever name it has, and a second one. The fake saves its state with its keys sorted, so
    # interface.query gives the ports by name: the second's, eth1, sorts after any en* name the fake's own may have.
    fake.s["interfaces"][next(iter(fake.s["interfaces"]))].update(media="2500Mb/s Twisted Pair")
    second = {"ipv4_dhcp": False, "ipv6_auto": False, "aliases": [], "live": [], "media": "1000Mb/s Twisted Pair"}
    fake.s["interfaces"]["eth1"] = second
    fake.s["temperatures"]["nvme3n1"] = [74.0, 80.85]
    persistent = next(p for p in fake.s["pools"] if p["name"] == "persistent")
    persistent["topology"]["data"][0]["children"][1]["stats"]["checksum_errors"] = 3
    next(s for s in fake.s["services"] if s["service"] == "ssh").update(state="RUNNING")
    fake.s["nfs3_clients"] = [{"ip": "192.0.2.20", "export": "/mnt/persistent/nextcloud"}]
    states = [{"1": {"type": "open", "access": "rw", "filename": NFS_OPEN_FILE}}]
    # The kernel keeps a client that stopped renewing its lease as courtesy for up to a day.
    clients = [
        ("192.0.2.31:812", "confirmed"),
        ("198.51.100.7:740", "confirmed"),
        ("192.0.2.20:801", "confirmed"),
        ("192.0.2.20:802", "confirmed"),
        ("198.51.100.8:741", "courtesy"),
    ]
    fake.s["nfs4_clients"] = [
        {"id": str(n), "info": {"address": address, "status": status, "name": NFS_CLIENT_NAME}, "states": states}
        for n, (address, status) in enumerate(clients)
    ]
    fake.s["clock_offset"] = -95
    fake.s["refused"] = list(refused)
    fake.s["mutations"] = 0
    fake.save()


def test_the_nas_sections_details(nas_tree, nas, tmp_path):
    """The ports, each disk's temperature and errors by pool, who has the NFS shares mounted, a service running though
    configured stopped, and the NAS's clock, from three more reads in the same login, nothing but reads, and no disk's
    serial or model, no client's own name and no file it holds open in the report. Then the disks' temperatures and
    the NFSv4 clients refused: the NAS is read all the same, without them, saying so."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"])
    seed_details(nas["state"])
    nas["calls"].write_text("")
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 1, out + err
    assert "nas ok " in reading(out), out
    lines = out.splitlines()
    assert " Network    2.5 Gb/s · 1 Gb/s · 2 of 2 ports up" in lines, out
    disks = lines.index(" Disks °C   persistent  nvme1n1 36 · nvme2n1 37 (3 checksum) · nvme3n1 74")
    assert lines[disks + 1 : disks + 3] == ["            ephemeral   sda 30", "            boot-pool   nvme0n1 45"], out
    # The line wraps between its clients.
    nfs = lines.index(" NFS        running · 1 share · mounted by the Proxmox host (2 clients) · homeserver-worker-1")
    assert lines[nfs + 1] == "            198.51.100.7", out
    assert " Services   ssh running, configured stopped" in lines, out
    # The section's lines after its header, which may also say the address the NAS answered at.
    header = next(i for i, line in enumerate(lines) if re.fullmatch(NAS_HEADER, line))
    keys = [line[:12].strip() for line in lines[header + 1 :]]
    assert [k for k in keys[: keys.index("Alerts") + 1] if k] == [
        "Network",
        "Pools",
        "Disks °C",
        "Snapshots",
        "NFS",
        "Services",
        "Alerts",
    ], out
    attention = [(a["level"], a["subject"], a["text"]) for a in report["attention"]]
    # A disk's errors are its pool's problem: ZFS calls the pool not healthy.
    unhealthy = f"the pool is ONLINE, with errors on nvme2n1 (3 checksum): {fake_truenas.FAILING_DEV}"
    assert attention == [
        ("problem", "persistent", unhealthy),
        ("problem", "nfs", "an NFS share is mounted from 198.51.100.7, which is no machine of this setup"),
        ("problem", "nas", "ssh is running, though the configuration keeps it stopped"),
        ("todo", "nas", "the NAS's clock is 95 s behind this machine's"),
        ("todo", "nvme3n1", "the disk is at 74 °C, within 10 °C of its critical temperature of 81 °C"),
    ], report["attention"]
    # Each with what deals with it, from status_commands.
    commands = [a["command"] for a in report["attention"]]
    assert commands[0].startswith("each disk's SMART results on the NAS's Storage page"), commands
    assert commands[1].startswith("who holds the address"), commands
    assert commands[2] == "just deploy truenas", commands
    assert commands[3].startswith("the NAS's NTP servers"), commands
    assert commands[4].startswith("the NAS's fans and airflow"), commands
    found = report["nas"]
    assert found["link"] == {"ports": 2, "up": 2, "speeds": ["2.5 Gb/s", "1 Gb/s"], "mtu": 1500}
    assert [(d["name"], d["pool"], d["temp"], d["critical"], d["checksum_errors"]) for d in found["disks"]] == [
        ("nvme1n1", "persistent", 36, 81, 0),
        ("nvme2n1", "persistent", 37, 81, 3),
        ("nvme3n1", "persistent", 74, 81, 0),
        ("sda", "ephemeral", 30, 71, 0),
        ("nvme0n1", "boot-pool", 45, 81, 0),
    ]
    assert found["nfs"]["clients"] == [
        {"address": "192.0.2.20", "name": "the Proxmox host", "clients": 2},
        {"address": "192.0.2.31", "name": "homeserver-worker-1", "clients": 1},
        {"address": "198.51.100.7", "name": "", "clients": 1},
    ]
    assert (found["unexpected_clients"], found["unexpected_services"]) == (["198.51.100.7"], ["ssh"])
    assert found["clock_drift"] == -95 and found["errors"] == []
    # One login and nothing but reads, the three new ones once each: every disk's temperature with its threshold.
    logged = [json.loads(line) for line in nas["calls"].read_text().splitlines()]
    calls = [call["method"] for call in logged]
    assert calls.count("auth.login_with_api_key") == 1 and set(calls) <= NAS_READS, calls
    new = [(call["method"], call["params"]) for call in logged if call["method"].startswith(("disk.t", "nfs."))]
    # Of each NFSv4 client, its address and status alone: its own name and the files it holds open stay on the NAS.
    select = [[], {"select": ["info.address", "info.status"]}]
    assert new == [("disk.temperatures", [[], True]), ("nfs.get_nfs3_clients", []), ("nfs.get_nfs4_clients", select)]
    assert json.loads(nas["state"].read_text())["mutations"] == 0
    written = (nas_tree / ".cache" / "status" / "report.json").read_text()
    for secret in (fake_truenas.DISK_SERIAL, fake_truenas.DISK_MODEL, NFS_CLIENT_NAME, NFS_OPEN_FILE):
        assert secret not in out + err + written, secret

    seed_nas(nas["state"])
    seed_details(nas["state"], refused=["disk.temperatures", "nfs.get_nfs4_clients"])
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 1, out + err
    assert "nas ok " in reading(out), out
    lines = out.splitlines()
    assert " Disks      persistent  nvme1n1 · nvme2n1 (3 checksum) · nvme3n1" in lines, out
    assert " NFS        running · 1 share" in lines, out
    assert " not read: the disks' temperatures: disk.temperatures: [EPERM] Not authorized" in lines, out
    assert " not read: the NFS clients: nfs.get_nfs4_clients: [EPERM] Not authorized" in lines, out
    assert [(a["level"], a["subject"]) for a in report["attention"]] == [
        ("problem", "persistent"),
        ("problem", "nas"),
        ("todo", "nas"),
    ], report["attention"]
    assert report["nas"]["nfs"]["clients"] is None and report["nas"]["temperatures_read"] is False


# How TrueNAS 25.10.7 words a pool's state when a disk is gone (py-libzfs's MISSING_DEV_R).
MISSING = (
    "One or more devices could not be opened. Sufficient replicas exist for the pool to continue functioning in a "
    "degraded state."
)


def test_no_disks_serial_or_model_reaches_the_report(nas_tree, nas, tmp_path):
    """TrueNAS's alerts that quote a disk's serial or model, as its alert sources word them: nvme1n1 at its critical
    temperature, sda's SMART self-test failed, and persistent degraded by nvme3n1, gone, which TrueNAS names by the
    model and serial it last knew there. Each comes out by the disk's device name, in the text, the report and --json
    alike."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"])
    fake = fake_truenas.FakeTrueNAS(nas["state"])
    fake.load()
    persistent = next(p for p in fake.s["pools"] if p["name"] == "persistent")
    gone = persistent["topology"]["data"][0]["children"][2]
    gone.update(disk=None, device=None, status="UNAVAIL", unavail_disk=fake.disk_entry("nvme3n1"))
    persistent.update(status="DEGRADED", healthy=False, status_code="MISSING_DEV_R", status_detail=MISSING)
    fake.s["temperatures"]["nvme1n1"] = [83.0, 80.85]
    hot = {"device": "/dev/nvme1n1", "serial": fake_truenas.disk_serial("nvme1n1"), "crit_threshold": 80.85, "temp": 83}
    devices = "<br>The following devices are not healthy:<ul><li>Disk {} {} is UNAVAIL</li></ul>"
    devices = devices.format(fake_truenas.DISK_MODEL, fake_truenas.disk_serial("nvme3n1"))
    degraded = {"volume": "persistent", "state": "DEGRADED", "status": MISSING, "devices": devices}
    fake.s["alerts"] = [
        fake_truenas.alert("DiskTemperatureTooHot", "CRITICAL", fake_truenas.DISK_TEMPERATURE_TOO_HOT, hot),
        fake_truenas.alert(
            "SMARTFailedSelfTest",
            "WARNING",
            fake_truenas.SMART_FAILED_SELFTEST,
            {"name": "sda", "serial": fake_truenas.disk_serial("sda")},
        ),
        fake_truenas.alert("VolumeStatus", "CRITICAL", fake_truenas.VOLUME_STATUS, degraded),
    ]
    fake.s["mutations"] = 0
    fake.save()
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 1, out + err
    assert [a["text"] for a in report["nas"]["alerts"]] == [
        "Disk /dev/nvme1n1 critical temperature threshold is 80 degrees celsius and the current temp is 83 degrees"
        " celsius",
        "sda failed a SMART selftest.",
        f"Pool persistent state is DEGRADED: {MISSING} The following devices are not healthy:Disk nvme3n1 is UNAVAIL",
    ]
    assert "TrueNAS alerts (warning): sda failed a SMART selftest." in [a["text"] for a in report["attention"]]
    # The gone disk by the name TrueNAS last knew there, with no reading.
    assert " Disks °C   persistent  nvme1n1 83 · nvme2n1 37 · nvme3n1 (gone, unavail)" in out.splitlines(), out
    rc, printed, json_err = just_status(nas_tree, state, "nas", "--json", **nas_vars(nas))
    assert rc == 1 and json.loads(printed)["nas"]["alerts"] == report["nas"]["alerts"], printed + json_err
    written = (nas_tree / ".cache" / "status" / "report.json").read_text()
    for secret in (fake_truenas.DISK_SERIAL, fake_truenas.DISK_MODEL):
        assert secret not in out + err + written + printed + json_err, secret
    assert json.loads(nas["state"].read_text())["mutations"] == 0


def test_a_nas_on_another_release_is_a_thing_to_do(nas_tree, nas, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"], version="26.04.1")
    rc, out, err = just_status(nas_tree, state, "nas", **nas_vars(nas))
    assert rc == 0, out + err
    todo = [a for a in report_of(nas_tree)["attention"] if a["subject"] == "nas"]
    assert [(a["level"], a["text"]) for a in todo] == [
        ("todo", "the NAS runs TrueNAS 26.04.1, and its configuration is written for 25.10")
    ]
    assert todo[0]["command"].startswith("just deploy truenas refuses it until truenas_supported_version")


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
    rc, out, err = just_status(nas_tree, state, **nas_vars(nas, **given))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 2, out + err
    assert "nas not read " in reading(out), out
    unread = next(a for a in report["attention"] if a["subject"] == "nas")
    assert unread["level"] == "unread" and unread["text"].startswith("the NAS could not be read: ")
    assert why in unread["text"], unread
    assert "NAS  not read" in out and "auth.login_with_api_key" not in nas_calls(nas)


def test_the_sections_shown_read_only_the_machines_they_need(nas_tree, nas, tmp_path):
    """just status nas reads the NAS alone: a Proxmox host that would refuse the connection and another cluster's
    kubeconfig, which a read of either would report, are never read; and the VMs and the storage read no NAS."""
    state = tmp_path / "pve.json"
    seed(state)
    seed_nas(nas["state"])
    credentials(talos_dir(tmp_path), server="https://192.0.2.99:6443")
    (nas_tree / "unreachable.ini").write_text(
        f"[proxmox]\nhomeserver ansible_connection=ssh ansible_host=127.0.0.1 ansible_port={free_port()}\n"
        "[nas]\ntruenas\n[kubernetes]\nhomeserver-k8s\n"
    )
    rc, out, err = just_status(nas_tree, state, "nas", inventory="unreachable.ini", local=False, **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 0, out + err
    assert re.fullmatch(r"reading  nas ok \d+\.\d s", reading(out)), out
    assert report["proxmox"] == {} and report["cluster"] == {} and report["attention"] == [], report
    assert "NAS  truenas · TrueNAS 25.10.7" in out and "did not answer" not in out + err, out + err

    credentials(talos_dir(tmp_path))
    nas["calls"].write_text("")
    rc, out, err = just_status(nas_tree, state, "vms", "storage", **nas_vars(nas))
    assert rc == report_of(nas_tree)["exit"] == 0, out + err
    assert " nas " not in reading(out) and report_of(nas_tree)["nas"] == {} and nas_calls(nas) == [], out


# ------------------------------------------------------------------------------------------------ backups ---
DAY = 86400


def seed_backups(path, runs, vmids=(300, 301, 310), job=True, next_in=4 * 3600 - 30):
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
    rc, out, err = just_status(nas_tree, state, "backups", **nas_vars(nas))
    report = report_of(nas_tree)
    assert rc == report["exit"] == 1, out + err
    # The ages are the wall clock's: the run started 20 h before the report, give or take the run's own seconds.
    attention = [(a["level"], re.sub(r"\d\d h (ago|old)", r"NN h \1", a["text"])) for a in report["attention"]]
    assert attention == [
        ("problem", f"the backup of 301 homeserver-worker-1 failed NN h ago: {FREEZE}; the last good one is NN h old"),
        ("todo", "310 homeassistant has no backup yet: vm-nightly runs next in 3 h 59 m"),
    ], report["attention"]
    assert json.loads(state.read_text())["mutations"] == pve["mutations"]
    lines = [re.sub(r"\d\d h ago", "NN h ago", line) for line in out.splitlines()]
    assert "proxmox ok " in reading(out), out
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
        ["ADMIN", "VMS", "STORAGE", "CLUSTER", "APPS", "BACKUPS"],
        [
            ("todo", "upgrades"),
            ("todo", "restart"),
            ("todo", "local-lvm"),
            ("todo", "suspended"),
            ("todo", "allowlist"),
            ("todo", "backup"),
        ],
        0,
    ),
    "nas": (
        example_without_kubernetes(),
        ["ADMIN", "VMS", "NAS", "STORAGE"],
        [("todo", "upgrades"), ("todo", "local-lvm")],
        0,
    ),
    "nas-cluster": (
        None,
        ["ADMIN", "VMS", "NAS", "STORAGE", "CLUSTER", "APPS", "BACKUPS"],
        [
            ("todo", "upgrades"),
            ("todo", "restart"),
            ("todo", "local-lvm"),
            ("todo", "suspended"),
            ("todo", "allowlist"),
        ],
        0,
    ),
}


@pytest.mark.parametrize("setup", SETUPS)
def test_each_setup_reports_what_it_has(setup, nas, tmp_path):
    local_yml, sections, attention, code = SETUPS[setup]
    root = example_tree(tmp_path / "tree", local_yml)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "justfile", root)
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
    rc, out, err = just_status(root, state, cluster=setup.endswith("cluster"), **given)
    assert (root / ".cache" / "status" / "report.json").exists(), out + err
    report = report_of(root)
    headers = [m.group(1) for m in map(re.compile(r"([A-Z]{3,})(  |$)").match, out.splitlines()) if m]
    assert headers == [*sections, "ATTENTION"], out
    assert [(a["level"], a["subject"]) for a in report["attention"]] == attention, out
    assert rc == report["exit"] == code, out + err
    assert max(len(line) for line in out.splitlines()) <= 100, out
