"""roles/status's filters: what the Proxmox host's and the cluster's reads say, the report and its exit code, and the
report as text.

The shapes are those captured on 2026-10-21 from a Proxmox VE 9.2 host and the Talos cluster on it, written out here
with made-up names, addresses and sizes: /cluster/resources (its node, VMs, a template and storages), the node's
status, a day's history, pending changes, Home Assistant OS's and Talos's guest agents, a storage's volumes, and
apt-get's simulated upgrade; the cluster's nodes, Deployments (with their managed fields), pods, Services,
Ingresses, volumes, Flux's objects, a Certificate, kubelet's statistics, amtool's alerts and talosctl service etcd.
"""

import base64
import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "status" / "filter_plugins" / "status.py"
_spec = importlib.util.spec_from_file_location("status_filters", PLUGIN)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)

GIB = 2**30
NOW = 1792570320
# Colours, and the OSC 8 links a URL is wrapped in.
ANSI = re.compile(r"\033\[[0-9;]*m|\033\]8;;[^\033]*\033\\")


def ok(data):
    """A status_read result whose output is `data`, as JSON unless it is text already."""
    return {"rc": 0, "stdout": data if isinstance(data, str) else json.dumps(data), "stderr": "", "error": ""}


def failed(stderr, rc=255):
    return {"rc": rc, "stdout": "", "stderr": stderr, "error": ""}


def vm_entry(vmid, name, status="running", cores=2, mem=1, maxmem=4, uptime=19847, template=0):
    return {
        "id": f"qemu/{vmid}",
        "type": "qemu",
        "vmid": vmid,
        "node": "pve",
        "name": name,
        "status": status,
        "cpu": 0.13 if status == "running" else 0,
        "maxcpu": cores,
        "mem": mem * GIB,
        "maxmem": maxmem * GIB,
        "disk": 0,
        "maxdisk": 32 * GIB,
        "uptime": uptime,
        "template": template,
    }


def storage_entry(sid, kind, used, total, content="images,rootdir", status="available"):
    return {
        "id": f"storage/pve/{sid}",
        "type": "storage",
        "node": "pve",
        "storage": sid,
        "status": status,
        "plugintype": kind,
        "content": content,
        "shared": 1 if kind == "nfs" else 0,
        "disk": used * GIB,
        "maxdisk": total * GIB,
    }


RESOURCES = [
    vm_entry(300, "homeserver-cp-1", maxmem=4),
    vm_entry(301, "homeserver-worker-1", cores=4, mem=6, maxmem=8),
    vm_entry(310, "homeassistant", mem=2, maxmem=2, uptime=49801),
    vm_entry(9000, "ubuntu-template", status="stopped", template=1),
    vm_entry(9001, "another-project", cores=4, maxmem=8),
    {
        "id": "node/pve",
        "type": "node",
        "node": "pve",
        "status": "online",
        "cpu": 0.16,
        "maxcpu": 4,
        "mem": 12 * GIB,
        "maxmem": 16 * GIB,
        "disk": 10 * GIB,
        "maxdisk": 39 * GIB,
        "uptime": 1141465,
    },
    storage_entry("local", "dir", 10, 39, content="images,backup,iso,import,vztmpl"),
    storage_entry("local-lvm", "lvmthin", 21, 68),
    {"id": "network/pve/zone/localnetwork", "type": "network", "node": "pve", "status": "ok"},
]
NODE_STATUS = {
    "pveversion": "pve-manager/9.2.21/4f6e0ac86f9e8c7f",
    "current-kernel": {"machine": "x86_64", "release": "7.0.14-22-pve", "sysname": "Linux"},
    "uptime": 1141468,
    "rootfs": {"used": 10 * GIB, "total": 39 * GIB, "avail": 27 * GIB, "free": 29 * GIB},
    "memory": {"used": 12 * GIB, "total": 16 * GIB, "free": 4 * GIB, "available": 4 * GIB},
}


def history(cpu):
    """A day's history, a point a minute, the first carrying only its time."""
    return [{"time": NOW - 86400}] + [
        {"time": t, "cpu": cpu(t), "maxcpu": 4, "mem": GIB, "maxmem": 4 * GIB}
        for t in range(NOW - 86400 + 60, NOW + 1, 60)
    ]


PENDING = [
    {"key": "cores", "value": 4},
    {"key": "memory", "value": 7680, "pending": 8192},
    {"key": "watchdog", "value": "model=i6300esb,action=reset", "delete": 1},
]
HAOS_FSINFO = [
    {"mountpoint": "/tmp", "type": "ext4", "used-bytes": 65536, "total-bytes": 14540800, "disk": []},
    {"mountpoint": "/mnt/data", "type": "ext4", "used-bytes": 5310988288, "total-bytes": 31688658944, "disk": []},
    {"mountpoint": "/mnt/overlay", "type": "ext4", "used-bytes": 83968, "total-bytes": 81520640, "disk": []},
    {"mountpoint": "/", "type": "erofs", "used-bytes": 2 * GIB, "total-bytes": 2 * GIB, "disk": []},
]
HAOS_OSINFO = {"id": "haos", "name": "Home Assistant OS", "pretty-name": "Home Assistant OS 18.3", "version-id": "18.3"}
TALOS_OSINFO = {"kernel-release": "6.18.54-talos", "machine": "x86_64"}
CONTENT = [
    {"volid": "local-lvm:base-9000-disk-0", "vmid": 9000, "size": 3.5 * GIB, "content": "images", "format": "raw"},
    {"volid": "local-lvm:vm-300-disk-1", "vmid": 300, "size": 10 * GIB, "content": "images", "format": "raw"},
    {"volid": "local-lvm:vm-301-disk-1", "vmid": 301, "size": 32 * GIB, "content": "images", "format": "raw"},
    {"volid": "local-lvm:vm-301-disk-2", "vmid": 301, "size": 14 * GIB, "content": "images", "format": "raw"},
    {"volid": "local-lvm:vm-302-disk-0", "vmid": 302, "size": 8 * GIB, "content": "rootdir", "format": "raw"},
]
UPGRADES = (
    "NOTE: This is only a simulation!\n"
    "1 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
    "Inst proxmox-kernel-7.0.14-25-pve-signed (7.0.14-25 Proxmox:trixie [amd64])\n"
    "Inst pve-manager [9.2.20] (9.2.21 Proxmox:trixie [amd64])\n"
    "Conf pve-manager (9.2.21 Proxmox:trixie [amd64])\n"
)
VMS = [
    {
        "vmid": 300,
        "name": "homeserver-cp-1",
        "made_by": "make deploy TYPE=kubernetes",
        "start": "make resume ALL=1",
        "node": "homeserver-cp-1",
    },
    {
        "vmid": 301,
        "name": "homeserver-worker-1",
        "made_by": "make deploy TYPE=kubernetes",
        "start": "make resume ALL=1",
        "node": "homeserver-worker-1",
    },
    {
        "vmid": 310,
        "name": "homeassistant",
        "made_by": "make deploy TYPE=homeassistant",
        "start": "make resume ONLY=homeassistant",
        "guest_agent": True,
        "app": {"name": "homeassistant", "url": "", "port": 8123},
    },
]
COMMANDS = {
    "upgrade": "make deploy TYPE=proxmox",
    "resume": "make resume ONLY={names}",
    "talos_upgrade": "make check TYPE=talos-upgrade, then make talos-upgrade",
    "credentials": "make deploy TYPE=kubernetes",
    "allowlist": "the first visits, then make deploy TYPE=kubernetes",
    "secrets": "k8s/README-secrets.md",
}
# Home Assistant OS's interfaces, its Docker bridges and the loopback beside its own. The bridges' and the link-local
# addresses are built here: gitleaks flags them written out.
HAOS_NETWORK = [
    {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
    {
        "name": "hassio",
        "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": ".".join(["172", "30", "32", "1"]), "prefix": 23}],
    },
    {
        "name": "docker0",
        "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": ".".join(["172", "30", "232", "1"]), "prefix": 23}],
    },
    {
        "name": "enp0s18",
        "ip-addresses": [
            {"ip-address-type": "ipv6", "ip-address": "fe80" + "::1", "prefix": 64},
            {"ip-address-type": "ipv4", "ip-address": "192.0.2.32", "prefix": 24},
        ],
    },
]


def results(**more):
    out = {
        "resources": ok(RESOURCES),
        "node": ok(NODE_STATUS),
        "rrd-node": ok(history(lambda t: 0.16)),
        "rrd-300": ok(history(lambda t: 0.05)),
        "rrd-301": ok(history(lambda t: 0.9 if t > NOW - 3600 else 0.2)),
        "rrd-310": ok(history(lambda t: 0.01)),
        "pending-300": ok([{"key": "cores", "value": 2}]),
        "pending-301": ok(PENDING),
        "pending-310": ok([]),
        "fsinfo-310": ok(HAOS_FSINFO),
        "osinfo-310": ok(HAOS_OSINFO),
        "network-310": ok(HAOS_NETWORK),
        "content-local-lvm": ok(CONTENT),
        "updates": ok(UPGRADES),
        "reboot": {"rc": 0, "stdout": "", "stderr": "", "error": ""},
    }
    return out | more


def found(**more):
    return fs.proxmox(
        {"unread": "", "node": "pve", "results": results(**more)},
        VMS,
        ["local-lvm"],
        16,
        "/run/reboot-required",
    )


# ------------------------------------------------------------------------------------------------ reading ---


def test_a_day_of_cpu_in_twelve_spans():
    buckets = fs.cpu_buckets(history(lambda t: 0.9 if t > NOW - 3600 else 0.2))
    assert len(buckets) == 12 and buckets[:11] == [0.2] * 11 and 0.5 < buckets[11] < 0.6
    assert fs.cpu_buckets([]) == [] and fs.cpu_buckets([{"time": NOW}]) == [None] * 12


def test_a_vm_started_today_has_no_history_before_it():
    started = NOW - 3 * 3600
    points = [{"time": t} if t < started else {"time": t, "cpu": 0.5} for t in range(NOW - 86400, NOW + 1, 60)]
    buckets = fs.cpu_buckets(points)
    assert buckets[:10] == [None] * 10 and buckets[10:] == [0.5, 0.5]


def test_the_guest_agent_filesystems_worth_showing():
    assert fs.filesystems(HAOS_FSINFO) == [{"mount": "/mnt/data", "used": 5310988288, "total": 31688658944}]
    assert fs.filesystems([]) == [] and fs.filesystems(None) == []


def test_the_os_name_only_when_the_agent_says_it():
    assert fs.os_name(HAOS_OSINFO) == "Home Assistant OS 18.3"
    assert fs.os_name(TALOS_OSINFO) == "" and fs.os_name(None) == ""


def test_pending_settings():
    assert fs.pending_keys(PENDING) == ["memory", "watchdog"]
    assert fs.pending_keys([]) == [] and fs.pending_keys(None) == []


def test_upgrades_from_a_simulation():
    assert fs.upgrades(UPGRADES) == [
        {"package": "proxmox-kernel-7.0.14-25-pve-signed", "old": "", "new": "7.0.14-25"},
        {"package": "pve-manager", "old": "9.2.20", "new": "9.2.21"},
    ]
    assert fs.upgrades("0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n") == []


def test_the_host():
    host = found()
    assert (host["node"], host["version"], host["kernel"]) == ("pve", "9.2.21", "7.0.14-22-pve")
    assert host["host"]["memory"] == {"used": 12 * GIB, "total": 16 * GIB}
    assert host["host"]["root"] == {"used": 10 * GIB, "total": 39 * GIB}
    assert host["host"]["cores"] == 4 and len(host["host"]["cpu_24h"]) == 12
    assert host["reboot"] is True and len(host["upgrades"]) == 2 and host["errors"] == []


def test_this_setups_vms_and_the_others_counted():
    vms = {vm["vmid"]: vm for vm in found()["vms"]}
    assert list(vms) == [300, 301, 310]
    assert vms[301]["pending"] == ["memory", "watchdog"] and vms[300]["pending"] == []
    assert vms[301]["memory"] == {"used": 6 * GIB, "total": 8 * GIB, "view": "host"}
    assert vms[310]["filesystems"][0]["mount"] == "/mnt/data" and vms[310]["os"] == "Home Assistant OS 18.3"
    # Talos's agent is never asked: it lists no filesystem.
    assert vms[300]["filesystems"] == [] and vms[300]["os"] == "" and vms[300]["agent"] == ""
    # The nodes are the cluster's, and Home Assistant an app at the address its agent reports, past its bridges.
    assert vms[300]["node"] == "homeserver-cp-1" and "node" not in vms[310]
    assert vms[310]["app"] == {"name": "homeassistant", "url": "http://192.0.2.32:8123"}
    assert found()["others"] == {"vmids": [9001], "templates": [9000], "running": 1, "cores": 4, "memory": 8 * GIB}


def test_storages_and_the_disk_budget():
    storages = {s["id"]: s for s in found()["storages"]}
    assert list(storages) == ["local", "local-lvm"]
    # local-lvm is budgeted: every guest's disks and volumes, other projects' too, against its whole size (thin LVM
    # keeps no headroom for the host). local is not in the budgeted list.
    assert storages["local-lvm"]["budget"] == {"provisioned": int(67.5 * GIB), "limit": 68 * GIB}
    assert storages["local"]["budget"] is None


def test_a_budgeted_directory_keeps_the_hosts_headroom():
    host = fs.proxmox(
        {"unread": "", "node": "pve", "results": results(**{"content-local": ok(CONTENT)})}, VMS, ["local"], 16, ""
    )
    local = next(s for s in host["storages"] if s["id"] == "local")
    assert local["budget"]["limit"] == (39 - 16) * GIB


def test_a_vm_that_is_missing_or_stopped():
    stopped = [vm_entry(300, "homeserver-cp-1", status="stopped"), *RESOURCES[1:]]
    vms = {vm["vmid"]: vm for vm in found(resources=ok(stopped[:2] + stopped[3:]))["vms"]}
    assert vms[300]["status"] == "stopped" and vms[310]["status"] == "missing"
    assert vms[310]["name"] == "homeassistant" and "uptime" not in vms[310]


def test_a_guest_agent_that_does_not_answer():
    vm = found(**{"fsinfo-310": failed("QEMU guest agent is not running")})["vms"][2]
    assert vm["agent"] == "QEMU guest agent is not running" and vm["filesystems"] == []


def test_a_host_that_does_not_answer_or_has_no_api():
    assert fs.proxmox({"unread": "it did not answer"}, VMS) == {"unread": "it did not answer"}
    host = fs.proxmox(
        {"unread": "", "node": "pve", "results": {"resources": failed("pvesh: command not found", 127)}}, VMS
    )
    assert host == {"unread": "pvesh get /cluster/resources: pvesh: command not found"}


def test_partial_failures_are_listed_not_fatal():
    host = found(node=failed("node status unavailable"), updates={"rc": None, "error": "timed out after 30 s"})
    assert host["errors"] == [
        "the node's status: node status unavailable",
        "the pending upgrades: timed out after 30 s",
    ]
    assert host["upgrades"] is None and host["version"] == ""


def test_no_reboot_flag():
    assert found(reboot={"rc": 1, "stdout": "", "stderr": "", "error": ""})["reboot"] is False
    assert fs.proxmox({"unread": "", "node": "pve", "results": results()}, VMS, [], 0, "")["reboot"] is None


def test_a_guest_address_past_its_bridges():
    assert fs.guest_address(HAOS_NETWORK) == "192.0.2.32"
    assert fs.guest_address(HAOS_NETWORK[:3]) == "" and fs.guest_address(None) == ""


def test_an_app_vm_with_its_own_url_or_none_yet():
    with_url = [{**VMS[2], "app": {"name": "homeassistant", "url": "http://homeassistant.home.arpa/"}}]
    host = fs.proxmox({"unread": "", "node": "pve", "results": results()}, with_url)
    assert host["vms"][0]["app"]["url"] == "http://homeassistant.home.arpa/"
    port_80 = [{**VMS[2], "app": {"name": "homeassistant", "url": "", "port": 80}}]
    host = fs.proxmox({"unread": "", "node": "pve", "results": results()}, port_80)
    assert host["vms"][0]["app"]["url"] == "http://192.0.2.32"
    host = fs.proxmox({"unread": "", "node": "pve", "results": results(**{"network-310": failed("no agent")})}, VMS)
    assert host["vms"][2]["app"] == {"name": "homeassistant", "url": ""}


# ------------------------------------------------------------------------------------------------- report ---

# A setup whose cluster these tests leave out, so that the Proxmox host's findings are the report's.
SETUP = {"nas": False, "cluster": False, "home_assistant": True}


def test_what_needs_attention_and_the_exit_code():
    model = fs.report({"proxmox": found()}, SETUP, [], "2026-10-21 14:40 PDT", commands=COMMANDS)
    subjects = [(a["level"], a["subject"]) for a in model["attention"]]
    assert subjects == [("todo", "reboot"), ("todo", "upgrades"), ("todo", "restart")]
    assert model["attention"][1]["command"] == "make deploy TYPE=proxmox"
    assert model["attention"][2]["text"] == "301 homeserver-worker-1 waits for a restart to apply memory, watchdog"
    assert model["attention"][2]["command"] == "qm reboot 301"
    assert model["exit"] == 0 and model["types"] == ["apps", "admin", "vms", "storage", "cluster", "nas"]
    # A command not given is suggested by text alone.
    assert fs.report({"proxmox": found()}, SETUP)["attention"][1]["command"] == ""


def test_stopped_and_missing_vms_say_what_starts_or_makes_them():
    stopped = [
        vm_entry(300, "homeserver-cp-1", status="stopped"),
        vm_entry(301, "homeserver-worker-1", status="stopped"),
    ]
    model = fs.report({"proxmox": found(resources=ok(stopped + RESOURCES[3:]))}, SETUP)
    items = {a["subject"]: a for a in model["attention"]}
    assert items["stopped"]["text"] == "300 homeserver-cp-1 and 301 homeserver-worker-1 are stopped"
    assert items["stopped"]["command"] == "make resume ALL=1"
    assert items["missing"]["text"] == "VM 310 homeassistant is not on this host"
    assert items["missing"]["command"] == "make deploy TYPE=homeassistant"


def test_a_full_storage_is_a_problem():
    full = [*RESOURCES[:6], storage_entry("local", "dir", 36, 39), storage_entry("local-lvm", "lvmthin", 21, 68)]
    model = fs.report({"proxmox": found(resources=ok(full))}, SETUP)
    assert model["attention"][0]["level"] == "problem" and model["attention"][0]["subject"] == "local"
    assert model["exit"] == 1


def test_an_overcommitted_storage_and_one_that_is_down():
    over = [
        *RESOURCES[:6],
        storage_entry("local", "dir", 10, 39, status="unknown"),
        storage_entry("local-lvm", "lvmthin", 21, 40),
    ]
    model = fs.report({"proxmox": found(resources=ok(over))}, SETUP)
    problems = [a for a in model["attention"] if a["level"] == "problem"]
    assert [(a["subject"], a["text"]) for a in problems] == [("local", "the storage is unknown")]
    overcommitted = next(a for a in model["attention"] if a["subject"] == "local-lvm")
    assert overcommitted["level"] == "todo" and "more than the 40 GiB it can hold" in overcommitted["text"]


def test_a_source_not_read_comes_first_and_exits_2():
    model = fs.report({"proxmox": {"unread": "it did not answer"}}, SETUP)
    assert model["attention"] == [
        {
            "level": "unread",
            "subject": "proxmox",
            "text": "the Proxmox host could not be read: it did not answer",
            "command": "",
        }
    ]
    assert model["exit"] == 2
    assert fs.report({}, SETUP)["exit"] == 2


def test_types_narrow_the_report():
    model = fs.report({"proxmox": found()}, SETUP, ["storage"])
    assert model["types"] == ["storage"] and model["attention"] == []
    assert fs.unknown_types(["vms", "apps", "nas", "disks"]) == ["disks"]
    assert fs.types_shown([]) == ["apps", "admin", "vms", "storage", "cluster", "nas"]
    assert fs.types_shown(["storage", "vms"]) == ["vms", "storage"]


def test_the_report_is_json():
    model = fs.report({"proxmox": found()}, SETUP, [], "2026-10-21 14:40 PDT")
    assert json.loads(json.dumps(model)) == model


# --------------------------------------------------------------------------------------------------- text ---


def report_text(color=False, **more):
    model = fs.report({"proxmox": found(**more)}, SETUP, [], "2026-10-21 14:40 PDT", commands=COMMANDS)
    return fs.text(model, color)


def test_the_text_fits_a_100_column_terminal_and_lines_up():
    lines = report_text().splitlines()
    assert max(len(line) for line in lines) <= 100, "\n".join(lines)
    header = next(line for line in lines if line.lstrip().startswith("VMID"))
    worker = next(line for line in lines if "homeserver-worker-1" in line and "%" in line)
    for title in ("UP", "LAST 24 H", "DISKS"):
        column = header.index(title)
        assert worker[column - 1] == " " and worker[column] != " ", (title, header, worker)
    # Memory is right-aligned on its slash, under the column's title.
    assert worker[header.index("MEM GiB") :].startswith(" 6.0 / 8.0"), (header, worker)


def test_the_text_says_what_matters():
    out = report_text()
    assert out.startswith("homeserver  2026-10-21 14:40 PDT · Proxmox only + Home Assistant\n")
    assert "ATTENTION  0 problems · 3 to do" in out
    assert "→ qm reboot 301" in out and "→ make deploy TYPE=proxmox" in out
    assert "VMS  pve · Proxmox VE 9.2.21 · kernel 7.0.14-22-pve · up 13 d" in out
    assert "/mnt/data 4.9 / 30" in out and "Home Assistant OS 18.3" in out
    assert "+ 1 other VM (9001), 4 cores, 8.0 GiB, 1 running and 1 template (9000), not this repo's" in out
    assert "the host's view: ballooning is off" in out
    assert "disks 68 of 68 GiB budgeted" in out
    assert out.rstrip().endswith(
        "exit 0 · nothing is wrong · make status TYPE=apps|admin|vms|storage|cluster|nas · JSON=1"
    )


def test_long_messages_wrap_under_their_subject():
    unread = {
        "unread": "Task failed: Failed to connect to the host via ssh: ssh: connect to host 192.0.2.20 port 22: "
        "Connection timed out, after the host stopped answering"
    }
    model = fs.report({"proxmox": unread}, SETUP)
    lines = fs.text(model, False).splitlines()
    assert max(len(line) for line in lines) <= 100
    first = next(i for i, line in enumerate(lines) if "could not be read" in line)
    assert lines[first + 1].startswith(" " * 15) and lines[first + 1].strip()


def test_colour_only_when_asked():
    plain, coloured = report_text(), report_text(color=True)
    assert "\033[" not in plain and "\033[" in coloured
    assert ANSI.sub("", coloured) == plain


def test_the_text_of_a_host_not_read():
    out = fs.text(fs.report({"proxmox": {"unread": "it did not answer"}}, SETUP), False)
    assert "ATTENTION  1 problem · 0 to do" in out and "could not be read: it did not answer" in out
    assert "VMS" not in out and out.rstrip().endswith(
        "exit 2 · 1 source not read · make status TYPE=apps|admin|vms|storage|cluster|nas · JSON=1"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0 m"),
        (59 * 60, "59 m"),
        (5 * 3600 + 30 * 60, "5 h 30 m"),
        (6 * 86400 + 4 * 3600, "6 d 4 h"),
        (13 * 86400, "13 d"),
    ],
)
def test_uptime(seconds, text):
    assert fs._uptime(seconds) == text


# ------------------------------------------------------------------------------------------- the cluster ---

TRAEFIK = "192.0.2.40"
COMMIT = "7821488ed966940b57a3a7907bcad8109e76a867"
FLUX = "kustomize.toolkit.fluxcd.io"
DATA = "/var/mnt/local-path-provisioner"
VOLUMES = {DATA: "data", "/var/mnt/media": "media"}


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def deployment(namespace, name, image, owner="", replicas=1, ready=None, labels=None, annotations=None, **more):
    selector = {"app.kubernetes.io/name": more.pop("app", name)}
    flux = {f"{FLUX}/name": owner, f"{FLUX}/namespace": "flux-system"} if owner else {}
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {**selector, **flux, **(labels or {})},
            "annotations": annotations or {},
            "managedFields": more.pop("managed", []),
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": selector},
                "spec": {"containers": more.pop("containers", None) or [{"name": name, "image": image}]},
            },
        },
        "status": {
            "replicas": replicas,
            "readyReplicas": replicas if ready is None else ready,
            "conditions": more.pop("conditions", []),
        },
    }


def pod(namespace, app, waiting=None, restarts=0, finished=None, phase="Running", created=NOW - 3600, **more):
    container = {"name": app, "ready": not waiting, "restartCount": restarts, "state": {"running": {}}}
    if waiting:
        container["state"] = {"waiting": dict(zip(("reason", "message"), waiting, strict=False))}
    if finished:
        container["lastState"] = {"terminated": {"finishedAt": iso(finished)}}
    return {
        "metadata": {
            "name": f"{app}-559fdcf4b8-59p7n",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": app},
            "creationTimestamp": iso(created),
        },
        "spec": {"nodeName": "homeserver-worker-1", "containers": more.pop("containers", [{"name": app}])},
        "status": {"phase": phase, "conditions": more.pop("conditions", []), "containerStatuses": [container]},
    }


def service(namespace, name, selector, kind="ClusterIP", address="", ports=(("http", 80),)):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"type": kind, "selector": selector, "ports": [{"name": n, "port": p} for n, p in ports]},
        "status": {"loadBalancer": {"ingress": [{"ip": address, "ipMode": "VIP"}] if address else []}},
    }


def ingress(namespace, name, host, backend):
    path = {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": backend, "port": {"name": "http"}}}}
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"ingressClassName": "traefik", "rules": [{"host": host, "http": {"paths": [path]}}]},
        "status": {"loadBalancer": {"ingress": [{"ip": TRAEFIK}]}},
    }


def condition(ok=True, message="Applied revision: refs/heads/main@sha1:" + COMMIT, kind="Ready"):
    return {
        "type": kind,
        "status": "True" if ok else "False",
        "reason": "Succeeded" if ok else "Failed",
        "message": message,
        "lastTransitionTime": iso(NOW - 600),
    }


def flux_object(name, path="", ok=True, message=None, suspend=False, namespace="flux-system"):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"path": path, **({"suspend": True} if suspend else {})},
        "status": {
            "conditions": [condition(ok, message or ("Applied" if ok else "kustomize build failed"))],
            "lastAppliedRevision": f"refs/heads/main@sha1:{COMMIT}",
        },
    }


def node(name, control_plane=False, ready=True, talos="v1.14.2", kubelet="v1.37.1", pressure=(), gpu=None):
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    conditions += [{"type": c, "status": "True"} for c in pressure]
    allocatable = {"cpu": "3950m", "memory": "7033492Ki", "pods": "110", **({"gpu.intel.com/i915": gpu} if gpu else {})}
    return {
        "metadata": {"name": name, "labels": {"node-role.kubernetes.io/control-plane": ""} if control_plane else {}},
        "status": {
            "conditions": conditions,
            "nodeInfo": {"osImage": f"Talos ({talos})", "kubeletVersion": kubelet},
            "allocatable": allocatable,
        },
    }


def volume(claim, path):
    namespace, name = claim.split("/")
    return {
        "metadata": {"name": f"pvc-{name}"},
        "spec": {"local": {"path": path}, "claimRef": {"namespace": namespace, "name": name}},
    }


def stats(memory_used, memory_available, var_used, var_total, volumes=()):
    return {
        "node": {
            "memory": {"workingSetBytes": memory_used, "availableBytes": memory_available, "usageBytes": 1},
            "fs": {"usedBytes": var_used, "capacityBytes": var_total, "availableBytes": var_total - var_used},
        },
        "pods": [
            {
                "podRef": {"namespace": ns, "name": f"{name}-pod"},
                "volume": [
                    {"name": "v", "pvcRef": {"namespace": ns, "name": name}, "usedBytes": used, "capacityBytes": total}
                ],
            }
            for (ns, name), used, total in volumes
        ],
    }


SUSPENDED_AT = NOW - 2 * 86400
DEPLOYMENTS = [
    deployment("homepage", "homepage", "ghcr.io/gethomepage/homepage:v1.13.2@sha256:ab12", "apps-homepage", 2),
    deployment("media", "jellyfin", "docker.io/linuxserver/jellyfin:10.11.11ubu2404-ls42@sha256:cd34", "apps-media"),
    deployment(
        "media",
        "sonarr",
        "docker.io/linuxserver/sonarr:4.0.19.2979-ls320",
        "apps-media",
        replicas=0,
        annotations={f"{FLUX}/reconcile": "disabled", "homeserver.internal/replicas": "1"},
        managed=[
            {
                "manager": "kustomize-controller",
                "operation": "Apply",
                "time": iso(NOW - 9 * 86400),
                "fieldsV1": {"f:metadata": {"f:labels": {}}},
            },
            {
                "manager": "homeserver-ansible",
                "operation": "Update",
                "time": iso(SUSPENDED_AT),
                "fieldsV1": {"f:metadata": {"f:annotations": {"f:homeserver.internal/replicas": {}}}},
            },
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
    deployment("frigate", "frigate", "ghcr.io/blakeblackshear/frigate:0.17.2", "apps-frigate"),
    # Grafana comes from its chart: no Flux label of an app's, and a sidecar as its first container.
    deployment(
        "monitoring",
        "kube-prometheus-stack-grafana",
        "",
        app="grafana",
        labels={"app.kubernetes.io/version": "13.2.3"},
        containers=[
            {"name": "grafana-sc-dashboard", "image": "quay.io/kiwigrid/k8s-sidecar:2.11.2"},
            {"name": "grafana", "image": "docker.io/grafana/grafana:13.2.3"},
        ],
    ),
    deployment("traefik", "traefik", "docker.io/traefik:v3.7.14", app="traefik"),
]
PODS = [
    pod("homepage", "homepage"),
    pod("homepage", "homepage"),
    pod("media", "jellyfin", containers=[{"name": "jellyfin", "resources": {"limits": {"gpu.intel.com/i915": "1"}}}]),
    pod("nextcloud", "nextcloud"),
    pod("nextcloud", "nextcloud-db"),
    pod("frigate", "frigate", containers=[{"name": "frigate", "resources": {"limits": {"gpu.intel.com/i915": "1"}}}]),
    pod("monitoring", "grafana"),
    pod("traefik", "traefik"),
]
SERVICES = [
    service("homepage", "homepage", {"app.kubernetes.io/name": "homepage"}),
    service("media", "jellyfin", {"app.kubernetes.io/name": "jellyfin"}),
    service("media", "sonarr", {"app.kubernetes.io/name": "sonarr"}),
    service("nextcloud", "nextcloud", {"app.kubernetes.io/name": "nextcloud"}),
    service("nextcloud", "nextcloud-db", {"app.kubernetes.io/name": "nextcloud-db"}, ports=(("mysql", 3306),)),
    service("frigate", "frigate", {"app.kubernetes.io/name": "frigate"}),
    service(
        "frigate",
        "frigate-rtsp",
        {"app.kubernetes.io/name": "frigate"},
        "LoadBalancer",
        "192.0.2.41",
        (("rtsp", 8554),),
    ),
    service(
        "monitoring",
        "kube-prometheus-stack-grafana",
        {"app.kubernetes.io/instance": "kube-prometheus-stack", "app.kubernetes.io/name": "grafana"},
    ),
    service(
        "traefik",
        "traefik",
        {"app.kubernetes.io/name": "traefik"},
        "LoadBalancer",
        TRAEFIK,
        (("web", 80), ("websecure", 443)),
    ),
]
# Grafana's pods carry its instance label too, as its chart's do.
DEPLOYMENTS[6]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/instance"] = "kube-prometheus-stack"
INGRESSES = [
    ingress("homepage", "homepage", "homeserver.internal", "homepage"),
    ingress("media", "jellyfin", "jellyfin.homeserver.internal", "jellyfin"),
    ingress("media", "sonarr", "sonarr.homeserver.internal", "sonarr"),
    ingress("nextcloud", "nextcloud", "nextcloud.homeserver.internal", "nextcloud"),
    ingress("frigate", "frigate", "frigate.homeserver.internal", "frigate"),
    ingress(
        "monitoring", "kube-prometheus-stack-grafana", "grafana.homeserver.internal", "kube-prometheus-stack-grafana"
    ),
]
KUSTOMIZATIONS = [
    flux_object("flux-system", "k8s/clusters/local"),
    flux_object("infra-controllers", "./k8s/infrastructure/controllers/local"),
    *(flux_object(f"apps-{g}", f"./k8s/apps/{g}") for g in ("frigate", "homepage", "media", "nextcloud")),
]
FLUX_INSTANCE = {
    "metadata": {"name": "flux", "namespace": "flux-system"},
    "spec": {
        "distribution": {"version": "2.9.x"},
        "sync": {"kind": "GitRepository", "path": "k8s/clusters/local", "ref": "refs/heads/main"},
    },
    "status": {
        "conditions": [condition(message="Reconciliation finished in 2s")],
        "lastAppliedRevision": "v2.9.6@sha256:c7b9402d",
    },
}
CERTIFICATE = {
    "metadata": {"name": "wildcard", "namespace": "traefik"},
    "spec": {"dnsNames": ["homeserver.internal", "*.homeserver.internal"], "secretName": "wildcard-tls"},
    "status": {
        "conditions": [condition(message="Certificate is up to date and has not expired")],
        "notAfter": iso(NOW + 89 * 86400),
        "renewalTime": iso(NOW + 59 * 86400),
    },
}
PVS = [
    volume("media/jellyfin-config", f"{DATA}/pvc-5861_media_jellyfin-config"),
    volume("nextcloud/nextcloud-config", f"{DATA}/pvc-222f_nextcloud_nextcloud-config"),
    volume("media/media", "/var/mnt/media/static/media"),
    # The NAS's, which kubelet reports as the NAS's share: never a node's volume.
    {
        "metadata": {"name": "nextcloud-data"},
        "spec": {"csi": {"driver": "nfs.csi.k8s.io"}, "claimRef": {"namespace": "nextcloud", "name": "nextcloud-data"}},
    },
]
SETTINGS = {"data": {"DOMAIN": "homeserver.internal", "INGRESS_ALLOWED_RANGE": "192.0.2.0/24"}}
ETCD = (
    "NODE     192.0.2.30\nID       etcd\nSTATE    Running\nHEALTH   OK\n"
    "EVENTS   [Running]: Health check successful (6h28m40s ago)\n"
)
WATCHDOG = {
    "labels": {"alertname": "Watchdog", "severity": "none"},
    "status": {"state": "active"},
    "startsAt": iso(NOW - 3600),
}
CLUSTER_NODES = [{"name": "homeserver-cp-1", "role": "controlplane"}, {"name": "homeserver-worker-1", "role": "worker"}]


def listed(items):
    return ok({"apiVersion": "v1", "kind": "List", "items": items})


def cluster_results(**more):
    out = {
        "nodes": listed([node("homeserver-cp-1", control_plane=True), node("homeserver-worker-1", gpu="2")]),
        "deployments": listed(DEPLOYMENTS),
        "pods": listed(PODS),
        "services": listed(SERVICES),
        "ingresses": listed(INGRESSES),
        "persistentvolumes": listed(PVS),
        "kustomizations": listed(KUSTOMIZATIONS),
        "helmreleases": listed([flux_object("kube-prometheus-stack", namespace="monitoring")]),
        "fluxinstances": listed([FLUX_INSTANCE]),
        "certificates": listed([CERTIFICATE]),
        "settings": ok(SETTINGS),
        "stats-homeserver-cp-1": ok(stats(2 * GIB, 2 * GIB, int(2.5 * GIB), int(7.8 * GIB))),
        "stats-homeserver-worker-1": ok(
            stats(
                4 * GIB,
                8 * GIB,
                9 * GIB,
                30 * GIB,
                [
                    (("media", "jellyfin-config"), 61 * GIB, 200 * GIB),
                    (("nextcloud", "nextcloud-config"), 61 * GIB, 200 * GIB),
                    (("media", "media"), 86 * GIB, 150 * GIB),
                    (("nextcloud", "nextcloud-data"), 400 * GIB, 7000 * GIB),
                ],
            )
        ),
        "alerts": ok([WATCHDOG]),
        "etcd": ok(ETCD),
        "remote": ok(f"{COMMIT}\trefs/heads/main\n"),
    }
    return out | more


ACCESS = {
    "server": "https://192.0.2.30:6443",
    "kubeconfig": iso(NOW + 341 * 86400),
    "talosconfig": iso(NOW + 341 * 86400),
}


def cluster_found(unread="", access=ACCESS, **more):
    return fs.cluster(
        {"unread": unread, "results": cluster_results(**more), "access": access},
        nodes=CLUSTER_NODES,
        apps_path="k8s/apps",
        annotation="homeserver.internal/replicas",
        database_label="app.kubernetes.io/component=database",
        volumes=VOLUMES,
        settings={"allowlist": "INGRESS_ALLOWED_RANGE"},
        pins={"talos": "v1.14.2", "kubernetes": "1.37.1"},
        kubeconfig="~/.talos/homeserver/kubeconfig",
        now=NOW,
    )


def test_the_nodes_from_kubelet():
    nodes = {n["name"]: n for n in cluster_found()["nodes"]}
    assert nodes["homeserver-cp-1"]["role"] == "controlplane" and nodes["homeserver-worker-1"]["role"] == "worker"
    worker = nodes["homeserver-worker-1"]
    assert (worker["talos"], worker["kubelet"], worker["ready"], worker["pressure"]) == ("v1.14.2", "v1.37.1", True, [])
    # Memory is the guest's own: what it uses, and what it uses plus what is available.
    assert worker["memory"] == {"used": 4 * GIB, "total": 12 * GIB}
    # /var, then each user volume a claim lies on, once however many claims do; the NAS's share is no node's.
    assert [(f["mount"], f["used"], f["total"]) for f in worker["filesystems"]] == [
        ("/var", 9 * GIB, 30 * GIB),
        ("data", 61 * GIB, 200 * GIB),
        ("media", 86 * GIB, 150 * GIB),
    ]
    assert worker["filesystems"][1]["claims"] == ["media/jellyfin-config", "nextcloud/nextcloud-config"]
    assert worker["gpus"] == [{"resource": "gpu.intel.com/i915", "count": 2, "used_by": ["frigate", "jellyfin"]}]
    assert nodes["homeserver-cp-1"]["gpus"] == []


def test_the_apps_and_what_finds_them():
    apps = {a["name"]: a for a in cluster_found()["apps"]}
    # Sorted by group, then name; Traefik, which no Ingress leads to and Flux does not apply as an app, is none.
    assert list(apps) == ["frigate", "homepage", "jellyfin", "sonarr", "grafana", "nextcloud"]
    assert apps["jellyfin"]["urls"] == [
        {"url": "https://jellyfin.homeserver.internal", "host": "jellyfin.homeserver.internal", "address": TRAEFIK}
    ]
    assert apps["homepage"]["urls"][0]["url"] == "https://homeserver.internal" and apps["homepage"]["wanted"] == 2
    # Grafana is found through its Ingress, Service and Deployment, its version from its label.
    assert (apps["grafana"]["group"], apps["grafana"]["deployment"]) == ("monitoring", "kube-prometheus-stack-grafana")
    assert apps["grafana"]["version"] == "13.2.3" and apps["grafana"]["kustomization"] == ""
    assert {a: apps[a]["version"] for a in ("homepage", "jellyfin", "sonarr", "nextcloud")} == {
        "homepage": "v1.13.2",
        "jellyfin": "10.11.11",
        "sonarr": "4.0.19",
        "nextcloud": "34.0.2",
    }
    # A database is listed under its app, never as one.
    assert [(d["name"], d["state"], d["version"]) for d in apps["nextcloud"]["databases"]] == [
        ("nextcloud-db", "running", "11.4.12")
    ]
    assert apps["frigate"]["streams"] == ["rtsp://192.0.2.41:8554"]
    assert apps["sonarr"]["state"] == "suspended" and apps["sonarr"]["since"] == iso(SUSPENDED_AT)
    assert all(apps[a]["state"] == "running" for a in ("frigate", "homepage", "jellyfin", "grafana", "nextcloud"))


def broken(**apps):
    """The cluster's Deployments and pods with these apps' not ready, each its pod as given ({} for none) and its
    Deployment's status changed by the pod's `deployment`."""
    namespaces = {d["metadata"]["name"]: d["metadata"]["namespace"] for d in DEPLOYMENTS}
    deployments = [
        {**d, "status": {**d["status"], "readyReplicas": 0, **apps[d["metadata"]["name"]].get("deployment", {})}}
        if d["metadata"]["name"] in apps
        else d
        for d in DEPLOYMENTS
    ]
    pods = [p for p in PODS if p["metadata"]["labels"]["app.kubernetes.io/name"] not in apps]
    for name, status in apps.items():
        status = {k: v for k, v in status.items() if k != "deployment"}
        if status:
            pods.append(pod(namespaces[name], name, **status))
    return {"deployments": listed(deployments), "pods": listed(pods)}


@pytest.mark.parametrize(
    ("pod_status", "state", "detail"),
    [
        (
            {"waiting": ("CrashLoopBackOff", "back-off 5m0s"), "restarts": 14, "finished": NOW - 120},
            "failing",
            "CrashLoopBackOff, 14 restarts, the last 2 m ago",
        ),
        ({"waiting": ("ImagePullBackOff", "Back-off pulling image")}, "failing", "ImagePullBackOff"),
        (
            {"waiting": ("CreateContainerConfigError", 'secret "jellyfin-secrets" not found')},
            "waiting",
            "waits for the Secret media/jellyfin-secrets",
        ),
        (
            {"phase": "Pending", "waiting": ("ContainerCreating", ""), "created": NOW - 1200},
            "failing",
            "Pending for 20 m",
        ),
        ({"phase": "Pending", "waiting": ("ContainerCreating", ""), "created": NOW - 60}, "starting", "0 of 1 ready"),
        (
            {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/2 nodes are available: 1 Insufficient memory.",
                    }
                ],
            },
            "failing",
            "Unschedulable: 0/2 nodes are available: 1 Insufficient memory.",
        ),
    ],
)
def test_an_app_that_is_not_running_says_why(pod_status, state, detail):
    apps = {a["name"]: a for a in cluster_found(**broken(jellyfin=pod_status))["apps"]}
    assert (apps["jellyfin"]["state"], apps["jellyfin"]["detail"]) == (state, detail)


def test_an_app_past_its_progress_deadline_is_failing():
    deadline = {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
    apps = {a["name"]: a for a in cluster_found(**broken(jellyfin={"deployment": {"conditions": [deadline]}}))["apps"]}
    assert (apps["jellyfin"]["state"], apps["jellyfin"]["detail"]) == (
        "failing",
        "0 of 1 ready past its progress deadline",
    )


def test_an_app_scaled_to_zero_by_hand_and_one_flux_did_not_apply():
    zero = [
        {**d, "spec": {**d["spec"], "replicas": 0}} if d["metadata"]["name"] == "jellyfin" else d for d in DEPLOYMENTS
    ]
    failed_ks = [
        *KUSTOMIZATIONS,
        flux_object(
            "apps-photoprism",
            "./k8s/apps/photoprism",
            ok=False,
            message="kustomize build failed: accumulating resources",
        ),
    ]
    apps = {a["name"]: a for a in cluster_found(deployments=listed(zero), kustomizations=listed(failed_ks))["apps"]}
    assert (apps["jellyfin"]["state"], apps["jellyfin"]["detail"]) == ("stopped", "scaled to zero")
    assert (apps["apps-photoprism"]["state"], apps["apps-photoprism"]["group"]) == ("not applied", "photoprism")
    assert apps["apps-photoprism"]["detail"] == "kustomize build failed: accumulating resources"


def test_flux_etcd_certificates_and_alerts():
    found = cluster_found(
        alerts=ok(
            [
                WATCHDOG,
                {
                    "labels": {
                        "alertname": "KubePodCrashLooping",
                        "severity": "warning",
                        "namespace": "media",
                        "pod": "jellyfin-0",
                    },
                    "status": {"state": "active"},
                    "startsAt": iso(NOW - 2820),
                },
                {"labels": {"alertname": "Silenced", "severity": "critical"}, "status": {"state": "suppressed"}},
            ]
        )
    )
    assert found["flux"] == {
        "version": "v2.9.6",
        "ready": True,
        "message": "Reconciliation finished in 2s",
        "ref": "refs/heads/main",
        "path": "k8s/clusters/local",
        "applied": COMMIT,
        "remote": COMMIT,
    }
    assert found["kustomizations"] == {"kind": "Kustomizations", "total": 6, "ready": 6, "failing": [], "suspended": []}
    assert found["etcd"] == {"state": "Running", "health": "OK"}
    assert found["certificates"][0]["name"] == "traefik/wildcard" and found["certificates"][0]["days_left"] == 89
    # The Watchdog fires by design, and a silenced alert is not active.
    assert found["alerts"] == [
        {"name": "KubePodCrashLooping", "severity": "warning", "subject": "media/jellyfin-0", "since": iso(NOW - 2820)}
    ]
    assert found["allowlist"] == "192.0.2.0/24" and found["access"]["kubeconfig_days"] == 341
    assert [b["name"] for b in found["balancers"]] == ["frigate-rtsp", "traefik"] and found["errors"] == []


def test_a_cluster_that_does_not_answer_or_answers_in_part():
    refused = failed("The connection to the server 192.0.2.30:6443 was refused", 1)
    assert cluster_found(nodes=refused) == {
        "unread": "the cluster's API: The connection to the server 192.0.2.30:6443 was refused"
    }
    assert cluster_found(unread="no kubeconfig at /home/me/.talos/homeserver/kubeconfig") == {
        "unread": "no kubeconfig at /home/me/.talos/homeserver/kubeconfig"
    }
    found = cluster_found(
        certificates=failed('error: the server doesn\'t have a resource type "certificates"', 1),
        remote={"rc": None, "error": "timed out after 15 s"},
        **{"stats-homeserver-cp-1": failed("Error from server (ServiceUnavailable)", 1)},
    )
    assert found["errors"] == [
        'the Certificates: error: the server doesn\'t have a resource type "certificates"',
        "homeserver-cp-1's kubelet statistics: Error from server (ServiceUnavailable)",
        "the branch Flux syncs: timed out after 15 s",
    ]
    assert found["flux"]["remote"] is None and found["nodes"][0]["memory"] is None


def make_certificate(days):
    """A real certificate, PEM and base64-encoded as a kubeconfig or talosconfig holds it."""
    from cryptography import x509 as cx509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = cx509.Name([cx509.NameAttribute(cx509.oid.NameOID.ORGANIZATION_NAME, "system:masters")])
    start = datetime.now(UTC)
    cert = (
        cx509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode(), cert.not_valid_after_utc


def test_credentials_give_their_server_and_expiry_and_nothing_else():
    crt, expiry = make_certificate(30)
    kubeconfig = {
        "clusters": [{"name": "homeserver", "cluster": {"server": "https://192.0.2.30:6443"}}],
        "users": [{"name": "admin", "user": {"client-certificate-data": crt, "client-key-data": "S0VZ"}}],
    }
    talosconfig = {"context": "homeserver", "contexts": {"homeserver": {"crt": crt, "key": "S0VZ"}}}
    found = fs.credentials(kubeconfig, talosconfig)
    assert found == {
        "server": "https://192.0.2.30:6443",
        "kubeconfig": expiry.isoformat(),
        "talosconfig": expiry.isoformat(),
    }
    assert fs.credentials({}, {}) == {"server": "", "kubeconfig": "", "talosconfig": ""}
    assert fs.credentials({"users": [{"user": {"client-certificate-data": "bm90IGEgY2VydA=="}}]})["kubeconfig"] == ""


def test_what_the_probes_ask():
    prox = found()
    targets = fs.probe_targets({"proxmox": prox, "cluster": cluster_found()})
    urls = [t["url"] for t in targets]
    assert urls[:2] == ["https://frigate.homeserver.internal", "http://frigate.homeserver.internal"]
    assert all(t["address"] == TRAEFIK for t in targets if "homeserver.internal" in t["url"])
    assert urls[-1] == "http://192.0.2.32:8123" and targets[-1]["address"] == ""
    assert len(urls) == len(set(urls)) == 2 * 6 + 1
    assert fs.probe_targets({"proxmox": prox, "cluster": cluster_found()}, ["vms"]) == []


# --------------------------------------------------------------------------------- the report, with the apps ---

FULL_SETUP = {"nas": False, "cluster": True, "home_assistant": True}
CERT = {
    "issuer": "CN=homeserver.internal private CA,O=homeserver",
    "names": ["*.homeserver.internal"],
    "not_after": iso(NOW + 89 * 86400),
}


def probe(url, status=200, resolved=(TRAEFIK,), location="", error="", certificate=None):
    secure = url.startswith("https://")
    return {
        "url": url,
        "address": TRAEFIK,
        "resolved": list(resolved),
        "dns": "" if resolved else "not known",
        "status": status,
        "location": location,
        "error": error,
        "certificate": (certificate or CERT) if secure and status else None,
    }


def probes(**overrides):
    """What the probes see when all is well: each https URL answers, each http one redirects to it, but Jellyfin's,
    which answers itself, and Home Assistant answers at its own address."""
    out = []
    for target in fs.probe_targets({"proxmox": found(), "cluster": cluster_found()}):
        url = target["url"]
        if url.startswith("https://"):
            out.append(probe(url, 302 if "jellyfin" in url else 200))
        elif "jellyfin" in url:
            out.append(probe(url, 302, location="/web/"))
        elif target["address"]:
            out.append(probe(url, 308, location=url.replace("http://", "https://")))
        else:
            out.append(probe(url, 200, resolved=("192.0.2.32",)) | {"address": "192.0.2.32"})
    return [overrides.get(p["url"], p) for p in out]


def full_report(types=(), cluster=None, seen=None, prox=None, setup=FULL_SETUP):
    return fs.report(
        {"proxmox": prox or found(), "cluster": cluster or cluster_found()},
        setup,
        types,
        "2026-10-21 14:32 PDT",
        probes=probes() if seen is None else seen,
        admin=[{"name": "Proxmox VE", "url": "https://192.0.2.20:8006"}],
        commands=COMMANDS,
        now=NOW,
    )


def test_the_apps_rows_with_what_they_answered():
    model = full_report()
    apps = {a["name"]: a for a in model["apps"]}
    assert apps["homepage"]["urls"] == [{"url": "https://homeserver.internal", "http": 200, "error": ""}]
    # Jellyfin answers plain HTTP itself, for TV apps, so its http URL is listed; the others redirect to https.
    assert [u["url"] for u in apps["jellyfin"]["urls"]] == [
        "https://jellyfin.homeserver.internal",
        "http://jellyfin.homeserver.internal",
    ]
    assert apps["frigate"]["urls"][1] == {"url": "rtsp://192.0.2.41:8554", "http": None, "error": ""}
    assert apps["homeassistant"] | {"urls": None} == apps["homeassistant"] | {
        "name": "homeassistant",
        "group": "VM 310",
        "state": "running",
        "version": "18.3",
        "urls": None,
    }
    assert apps["homeassistant"]["urls"] == [{"url": "http://192.0.2.32:8123", "http": 200, "error": ""}]
    assert model["dns"] == {"state": "ok", "address": TRAEFIK, "asked": 6, "missing": [], "wrong": {}}
    assert model["tls"]["certificate"] == "traefik/wildcard" and model["traefik"] == TRAEFIK


def test_the_guest_view_replaces_proxmoxs_for_the_nodes():
    model = full_report()
    vms = {vm["vmid"]: vm for vm in model["proxmox"]["vms"]}
    assert vms[301]["memory"] == {"used": 4 * GIB, "total": 12 * GIB, "view": "guest"}
    assert [f["mount"] for f in vms[301]["filesystems"]] == ["/var", "data", "media"]
    assert vms[310]["memory"]["view"] == "host"
    assert [v["id"] for v in model["volumes"]] == ["homeserver-worker-1 data", "homeserver-worker-1 media"]


def test_a_healthy_cluster_asks_for_nothing():
    model = full_report()
    subjects = [a["subject"] for a in model["attention"]]
    assert subjects == ["suspended", "reboot", "upgrades", "restart"], model["attention"]
    suspended = model["attention"][0]
    assert suspended["text"] == f"sonarr is suspended since {iso(SUSPENDED_AT)[:10]}"
    assert suspended["command"] == "make resume ONLY=sonarr" and model["exit"] == 0


def attention_of(model):
    return {(a["level"], a["subject"]): a for a in model["attention"]}


def test_what_the_apps_need():
    trouble = broken(
        jellyfin={"waiting": ("CrashLoopBackOff", ""), "restarts": 3, "finished": NOW - 60},
        nextcloud={"waiting": ("CreateContainerConfigError", 'secret "nextcloud-secrets" not found')},
    )
    model = full_report(cluster=cluster_found(**trouble))
    items = attention_of(model)
    failing = items[("problem", "jellyfin")]
    assert failing["text"] == "CrashLoopBackOff, 3 restarts, the last 1 m ago"
    assert (
        failing["command"]
        == "kubectl --kubeconfig ~/.talos/homeserver/kubeconfig -n media logs deploy/jellyfin --previous"
    )
    assert items[("todo", "nextcloud")]["text"] == "waits for the Secret nextcloud/nextcloud-secrets"
    assert items[("todo", "nextcloud")]["command"] == "k8s/README-secrets.md"
    assert model["exit"] == 1 and model["attention"][0]["level"] == "problem"


def test_what_the_probes_find_wrong():
    closed = cluster_found(settings=ok({"data": {"INGRESS_ALLOWED_RANGE": "127.0.0.1/32"}}))
    seen = probes(
        **{
            "https://jellyfin.homeserver.internal": probe("https://jellyfin.homeserver.internal", 502),
            "https://grafana.homeserver.internal": probe("https://grafana.homeserver.internal", 200, resolved=()),
            "https://nextcloud.homeserver.internal": probe(
                "https://nextcloud.homeserver.internal", resolved=("192.0.2.9",)
            ),
        }
    )
    items = attention_of(full_report(cluster=closed, seen=seen))
    assert items[("problem", "jellyfin")]["text"] == "answers 502 through Traefik, though its pods are ready"
    assert "Traefik lets in 127.0.0.1/32 alone" in items[("todo", "allowlist")]["text"]
    assert items[("todo", "allowlist")]["command"] == COMMANDS["allowlist"]
    dns = [a for a in full_report(cluster=closed, seen=seen)["attention"] if a["subject"] == "dns"]
    assert [a["text"] for a in dns] == [
        "grafana.homeserver.internal does not resolve on this machine",
        "nextcloud.homeserver.internal resolves to 192.0.2.9 here, not Traefik's 192.0.2.40",
    ]
    nowhere = [p | {"status": None, "error": "timed out", "certificate": None} for p in probes()]
    items = attention_of(full_report(seen=nowhere))
    assert items[("problem", "traefik")]["text"] == "192.0.2.40 did not answer from here: timed out"
    unresolved = [p | {"resolved": []} for p in probes()]
    model = full_report(seen=unresolved)
    assert model["dns"]["state"] == "missing"
    assert attention_of(model)[("todo", "dns")]["text"] == "the apps' names do not resolve on this machine"


def test_what_the_cluster_needs():
    nodes = listed(
        [
            node("homeserver-cp-1", True, talos="v1.14.1", kubelet="v1.37.0"),
            node("homeserver-worker-1", ready=False, pressure=("DiskPressure",)),
        ]
    )
    unhealthy = cluster_found(
        nodes=nodes,
        etcd=ok("NODE     192.0.2.30\nID       etcd\nSTATE    Running\nHEALTH   Fail\n"),
        kustomizations=listed(
            [
                *KUSTOMIZATIONS,
                flux_object("infra-tls", "./k8s/infrastructure/tls", ok=False, message="decryption failed: no age key"),
            ]
        ),
        services=listed(
            [*SERVICES[:-1], service("traefik", "traefik", {"app.kubernetes.io/name": "traefik"}, "LoadBalancer")]
        ),
        alerts=ok(
            [
                {
                    "labels": {"alertname": "KubeNodeNotReady", "severity": "critical", "node": "homeserver-worker-1"},
                    "status": {"state": "active"},
                    "startsAt": iso(NOW - 600),
                }
            ]
        ),
        access={"server": "x", "kubeconfig": iso(NOW + 20 * 86400), "talosconfig": iso(NOW - 86400)},
    )
    items = attention_of(full_report(cluster=unhealthy))
    assert items[("problem", "homeserver-worker-1")]["text"] in (
        "the node is not Ready",
        "the node reports DiskPressure",
    )
    assert items[("problem", "etcd")]["text"] == "etcd is Running, its health Fail"
    assert items[("problem", "flux")]["text"] == "flux-system/infra-tls is not ready: decryption failed: no age key"
    assert (
        items[("problem", "traefik")]["text"] == "the LoadBalancer traefik/traefik has no address: MetalLB gave it none"
    )
    assert items[("problem", "alert")]["text"] == "KubeNodeNotReady (critical) fires for homeserver-worker-1"
    assert items[("problem", "access")]["text"] == "the talosconfig's client certificate has expired"
    assert items[("todo", "access")]["text"] == "the kubeconfig's client certificate expires in 20 d"
    pins = items[("todo", "talos")]
    assert pins["text"] == (
        "the nodes run Talos v1.14.1, v1.14.2 where the pins say v1.14.2 and Kubernetes 1.37.0, 1.37.1 where the "
        "pins say 1.37.1"
    )
    assert pins["command"] == COMMANDS["talos_upgrade"]


def test_a_certificate_that_traefik_does_not_serve_or_that_runs_out():
    own = {"issuer": "CN=TRAEFIK DEFAULT CERT", "names": [], "not_after": iso(NOW + 300 * 86400)}
    seen = [p | {"certificate": own} if p["certificate"] else p for p in probes()]
    items = attention_of(full_report(seen=seen))
    assert items[("todo", "tls")]["text"] == (
        "Traefik serves a certificate from CN=TRAEFIK DEFAULT CERT, not the cluster's own"
    )
    stale = {**CERTIFICATE, "status": {**CERTIFICATE["status"], "notAfter": iso(NOW + 5 * 86400)}}
    items = attention_of(full_report(cluster=cluster_found(certificates=listed([stale]))))
    assert items[("problem", "tls")]["text"] == "traefik/wildcard expires in 5 d and was not renewed"


def test_a_cluster_not_read_and_one_whose_vms_are_stopped():
    model = full_report(cluster={"unread": "the cluster's API: connection refused"})
    assert model["attention"][0] == {
        "level": "unread",
        "subject": "cluster",
        "text": "the cluster could not be read: the cluster's API: connection refused",
        "command": "",
    }
    assert model["exit"] == 2 and model["apps"][-1]["name"] == "homeassistant"
    stopped = [
        vm_entry(300, "homeserver-cp-1", status="stopped"),
        vm_entry(301, "homeserver-worker-1", status="stopped"),
        *RESOURCES[2:],
    ]
    model = full_report(cluster={"unread": "the cluster's API: connection refused"}, prox=found(resources=ok(stopped)))
    assert model["exit"] == 0 and model["cluster"]["stopped"] == ["300 homeserver-cp-1", "301 homeserver-worker-1"]
    assert "CLUSTER  not read: 300 homeserver-cp-1 and 301 homeserver-worker-1 are stopped" in fs.text(model)
    # A setup without a cluster expects none.
    assert fs.report({"proxmox": found()}, SETUP)["exit"] == 0


def full_text(color=False, **more):
    return fs.text(full_report(**more), color)


def test_the_full_text_fits_and_lines_up():
    lines = full_text().splitlines()
    assert max(len(line) for line in lines) <= 100, "\n".join(lines)
    header = next(line for line in lines if line.startswith(" APP "))
    for title in ("GROUP", "STATE", "VERSION", "URL", "HTTP"):
        column = header.index(title)
        for row in (line for line in lines if line.startswith((" jellyfin ", " homeassistant "))):
            assert row[column - 1] == " " and row[column] != " ", (title, header, row)
    second = next(line for line in lines if "http://jellyfin.homeserver.internal" in line)
    assert second.index("http://") == header.index("URL") and second.rstrip().endswith("302")


def test_the_full_text_says_what_matters():
    out = full_text()
    assert out.startswith("homeserver  2026-10-21 14:32 PDT · Proxmox only + cluster + Home Assistant\n")
    assert "APPS  https://<app>.homeserver.internal → Traefik 192.0.2.40 · DNS ok · allowlist 192.0.2.0/24" in out
    assert " sonarr         media       ○ suspended   4.0.19    https://sonarr.homeserver.internal" in out
    assert "   database                 ● running     11.4.12" in out
    assert "                                                    rtsp://192.0.2.41:8554" in out
    assert "ADMIN\n Proxmox VE       https://192.0.2.20:8006\n" in out
    assert "CLUSTER  Talos v1.14.2 · Kubernetes v1.37.1 · Flux v2.9.6" in out
    assert " Nodes      ● homeserver-cp-1 Ready · ● homeserver-worker-1 Ready · etcd healthy" in out
    assert " Pins       Talos v1.14.2 as pinned · Kubernetes 1.37.1 as pinned" in out
    assert f" Flux       main@{COMMIT[:7]}, the same as the branch · 6 of 6 Kustomizations ready" in out
    assert " Addresses  frigate-rtsp 192.0.2.41 · traefik 192.0.2.40" in out
    assert " GPU        gpu.intel.com/i915 on homeserver-worker-1: 2 of 2 in use (frigate, jellyfin)" in out
    assert " TLS        Traefik serves traefik/wildcard, until " in out
    assert " Access     talosconfig valid for 341 d · kubeconfig valid for 341 d" in out
    assert " Alerts     none firing" in out
    assert " homeserver-worker-1 data   talos   " in out and "61 / 200 GiB · 2 volumes in use" in out
    assert "/var 9.0 / 30" in out and "4.0 / 12.0  " in out


def test_missing_dns_comes_with_hosts_lines():
    out = full_text(seen=[p | {"resolved": []} for p in probes()])
    assert "DNS missing" in out
    assert "   192.0.2.40  frigate.homeserver.internal grafana.homeserver.internal homeserver.internal" in out


def test_urls_are_links_in_colour():
    coloured = full_text(color=True)
    assert "\033]8;;https://jellyfin.homeserver.internal\033\\" in coloured
    assert ANSI.sub("", coloured) == full_text()


def test_the_full_report_is_json():
    model = full_report()
    assert json.loads(json.dumps(model)) == model


# ------------------------------------------------------------------------------------------------ the NAS ---

TIB = 2**40
NAS_NOW = 1792540800  # 2026-10-21 00:00 UTC
NAS_SETUP = {"nas": True, "cluster": False, "home_assistant": False}
NAS_COMMANDS = {
    "nas": "make deploy TYPE=truenas",
    "nas_release": "raise truenas_supported_version",
    "nas_network": "commit them on the NAS",
}
HOURLY = {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"}
DAILY = {"minute": "0", "hour": "0", "dom": "*", "month": "*", "dow": "*"}
WEEKLY = {"minute": "0", "hour": "0", "dom": "*", "month": "*", "dow": "0"}


def scan(end="2026-10-12T00:00:00+00:00", state="FINISHED", errors=0, function="SCRUB", percentage=100.0):
    return {
        "function": function,
        "state": state,
        "start_time": "2026-10-11T21:00:00+00:00",
        "end_time": end,
        "percentage": percentage,
        "errors": errors,
    }


def pool(name, used, available, layout="raidz1", **more):
    return {
        "name": name,
        "status": "ONLINE",
        "healthy": True,
        "detail": "",
        "layout": layout,
        "used": int(used * TIB),
        "available": int(available * TIB),
        "scan": scan(),
        "scrub": {"threshold": 35, "enabled": True},
        **more,
    }


def task(dataset, schedule, state="FINISHED", last_run="2026-10-20T23:00:02+00:00", error="", enabled=True):
    return {
        "id": 1,
        "dataset": dataset,
        "enabled": enabled,
        "schedule": schedule,
        "lifetime_value": 2,
        "lifetime_unit": "DAY",
        "state": state,
        "last_run": last_run,
        "error": error,
    }


def nas_state(pools=None, alerts=(), tasks=None, **more):
    """What truenas_info returns with status, as roles/truenas's status entry point registers it: the disks (with
    their serials) and the shares beside the health."""
    return {
        "changed": False,
        "api_host": "192.0.2.10",
        "version": "25.10.7",
        "ready": True,
        "hostname": "truenas",
        "network_pending_changes": False,
        "disks": [{"name": "sda", "serial": "SERIAL-MUST-NOT-LEAK", "model": "SSD"}],
        "services": {"nfs": {"enable": True, "state": "RUNNING"}, "ssh": {"enable": False, "state": "STOPPED"}},
        "nfs_shares": [{"path": f"/mnt/persistent/{n}", "enabled": True} for n in ("a", "b", "c")]
        + [{"path": "/mnt/ephemeral/off", "enabled": False}],
        "status": {
            "uptime_seconds": 34 * 86400 + 5,
            "pools": [pool("persistent", 2.2, 4.9), pool("ephemeral", 1.0, 2.6, layout="stripe")]
            if pools is None
            else pools,
            "alerts": list(alerts),
            "snapshot_tasks": [
                task("persistent/proxmox/vm", HOURLY),
                task("persistent/proxmox/vm", DAILY, last_run="2026-10-20T00:00:02+00:00"),
                task("persistent/nextcloud", HOURLY),
            ]
            if tasks is None
            else tasks,
        },
        **more,
    }


def nas_report(state=None, types=(), release="25.10", **found):
    """The report of the NAS beside a Proxmox host that holds nothing to report."""
    return fs.report(
        {"proxmox": {}, "nas": fs.nas(nas_state() if state is None else state, release), **found},
        NAS_SETUP,
        types,
        now=NAS_NOW,
        commands=NAS_COMMANDS,
    )


def attention(model):
    return [(a["level"], a["subject"], a["text"], a["command"]) for a in model["attention"]]


def test_the_nas_from_truenas_info():
    found = fs.nas(nas_state(), "25.10")
    assert {k: found[k] for k in ("name", "address", "version", "release", "ready", "pending_network")} == {
        "name": "truenas",
        "address": "192.0.2.10",
        "version": "25.10.7",
        "release": "25.10",
        "ready": True,
        "pending_network": False,
    }
    assert found["uptime"] == 34 * 86400 + 5
    persistent = found["pools"][0]
    assert persistent["used"] == int(2.2 * TIB) and persistent["total"] == int(2.2 * TIB) + int(4.9 * TIB)
    assert persistent["layout"] == "raidz1" and persistent["scan"]["errors"] == 0
    assert [(t["dataset"], t["every"]) for t in found["snapshots"]] == [
        ("persistent/proxmox/vm", "hourly"),
        ("persistent/proxmox/vm", "daily"),
        ("persistent/nextcloud", "hourly"),
    ]
    assert found["nfs"] == {"enabled": True, "state": "RUNNING", "shares": 3}
    assert "SERIAL-MUST-NOT-LEAK" not in json.dumps(found)


@pytest.mark.parametrize(
    ("schedule", "every"),
    [(HOURLY, "hourly"), (DAILY, "daily"), (WEEKLY, "weekly"), (DAILY | {"dom": "1"}, "monthly"), ({}, "hourly")],
)
def test_how_often_a_snapshot_task_runs(schedule, every):
    assert fs._every(schedule) == every


def test_alerts_lose_their_html_and_dismissed_ones_never_come():
    alert = {"klass": "X", "level": "warning", "text": "Pool <b>tank</b> state is DEGRADED:<br>one disk &amp; more"}
    found = fs.nas(nas_state(alerts=[alert | {"datetime": "2026-10-20T08:00:00+00:00"}]))
    assert found["alerts"] == [
        {
            "level": "WARNING",
            "klass": "X",
            "text": "Pool tank state is DEGRADED: one disk & more",
            "since": "2026-10-20T08:00:00+00:00",
        }
    ]


@pytest.mark.parametrize(
    ("state", "why"),
    [
        (
            {
                "failed": False,
                "msg": "TrueNAS did not answer on any of the given addresses",
                "errors": {"192.0.2.10": "timed out"},
            },
            "TrueNAS did not answer on any of the given addresses: 192.0.2.10: timed out",
        ),
        (
            {"failed": False, "msg": "TrueNAS at 192.0.2.10 rejected the API key."},
            "TrueNAS at 192.0.2.10 rejected the API key",
        ),
        ({}, "not read"),
        ({"api_host": "192.0.2.10"}, "not read"),
    ],
)
def test_a_nas_that_was_not_read(state, why):
    assert fs.nas(state) == {"unread": why}
    model = fs.report({"proxmox": {}, "nas": fs.nas(state)}, NAS_SETUP, now=NAS_NOW)
    assert model["exit"] == 2
    assert ("unread", "nas", f"the NAS could not be read: {why}", "") in attention(model)
    out = fs.text(model, False)
    assert "NAS  not read" in out


def test_a_setup_without_a_nas_reads_none_and_shows_none():
    model = fs.report({}, SETUP, now=NAS_NOW)
    assert not [a for a in model["attention"] if a["subject"] == "nas"] and model["nas"] == {}
    assert "NAS" not in fs.text(model, False).replace("Proxmox only", "")
    model = fs.report({}, NAS_SETUP, now=NAS_NOW)
    assert ("unread", "nas", "the NAS was not read", "") in attention(model)


def test_a_healthy_nas_asks_for_nothing():
    model = nas_report(types=["nas"])
    assert model["attention"] == [] and model["exit"] == 0
    assert model["nas"]["pools"][0]["scanned_ago"] == 9 * 86400
    assert [t["ago"] for t in model["nas"]["snapshots"]] == [3598, 86398, 3598]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            nas_state(
                pools=[
                    pool(
                        "persistent",
                        2.2,
                        4.9,
                        status="DEGRADED",
                        healthy=False,
                        detail="One or more devices are faulted.",
                    )
                ]
            ),
            ("problem", "persistent", "the pool is DEGRADED: One or more devices are faulted.", ""),
            id="degraded",
        ),
        pytest.param(
            nas_state(pools=[pool("persistent", 6.5, 0.5)]),
            ("problem", "persistent", "the pool is 93% full: 6.5 / 7.0 TiB", ""),
            id="full",
        ),
        pytest.param(
            nas_state(pools=[pool("persistent", 5.8, 1.2)]),
            ("todo", "persistent", "the pool is 83% full: 5.8 / 7.0 TiB", ""),
            id="filling",
        ),
        pytest.param(
            nas_state(pools=[pool("persistent", 2.2, 4.9, scan=scan(errors=3))]),
            ("problem", "persistent", "its last scrub found 3 errors", ""),
            id="scrub-errors",
        ),
        pytest.param(
            nas_state(pools=[pool("persistent", 2.2, 4.9, scan=scan(end="2026-09-01T00:00:00+00:00"))]),
            ("todo", "persistent", "its last scrub was 50 d ago, though its task scrubs every 35 d", ""),
            id="scrub-overdue",
        ),
        pytest.param(
            nas_state(pools=[pool("persistent", 2.2, 4.9, scrub=None)]),
            ("todo", "persistent", "no scrub task scrubs it", "make deploy TYPE=truenas"),
            id="no-scrub-task",
        ),
        pytest.param(
            nas_state(tasks=[task("persistent/nextcloud", HOURLY, state="ERROR", error="out of space")]),
            ("problem", "snapshots", "the hourly snapshots of persistent/nextcloud failed: out of space", ""),
            id="snapshots-failed",
        ),
        pytest.param(
            nas_state(alerts=[{"klass": "SMART", "level": "CRITICAL", "text": "Device sda: SMART test failed"}]),
            ("problem", "nas", "TrueNAS alerts (critical): Device sda: SMART test failed", ""),
            id="critical-alert",
        ),
        pytest.param(
            nas_state(alerts=[{"klass": "NTP", "level": "WARNING", "text": "NTP health check failed"}]),
            ("todo", "nas", "TrueNAS alerts (warning): NTP health check failed", ""),
            id="warning-alert",
        ),
        pytest.param(
            nas_state(services={"nfs": {"enable": True, "state": "STOPPED"}}),
            (
                "problem",
                "nfs",
                "the NFS service is stopped, so its 3 shares are not served",
                "make deploy TYPE=truenas",
            ),
            id="nfs-stopped",
        ),
        pytest.param(
            nas_state(version="26.04.1"),
            (
                "todo",
                "nas",
                "the NAS runs TrueNAS 26.04.1, and its configuration is written for 25.10",
                "raise truenas_supported_version",
            ),
            id="another-release",
        ),
        pytest.param(
            nas_state(network_pending_changes=True),
            ("todo", "nas", "TrueNAS holds interface changes that were never committed", "commit them on the NAS"),
            id="pending-network",
        ),
        pytest.param(
            nas_state(ready=False),
            ("problem", "nas", "TrueNAS is not ready: its middleware is still starting", ""),
            id="not-ready",
        ),
    ],
)
def test_what_the_nas_needs(state, expected):
    model = nas_report(state)
    assert attention(model) == [expected]
    assert model["exit"] == (1 if expected[0] == "problem" else 0)


def test_info_alerts_a_disabled_task_and_a_scrub_under_way_ask_for_nothing():
    state = nas_state(
        pools=[pool("persistent", 2.2, 4.9, scan=scan(end=None, state="SCANNING", percentage=45.2))],
        alerts=[{"klass": "Update", "level": "INFO", "text": "An update is available"}],
        tasks=[task("persistent/nextcloud", HOURLY, state="ERROR", error="x", enabled=False)],
    )
    assert nas_report(state)["attention"] == []


def test_the_nas_is_left_out_unless_shown():
    state = nas_state(ready=False)
    model = nas_report(state, types=["vms"])
    assert model["attention"] == [] and "NAS " not in fs.text(model, False)
    unread = fs.report({"proxmox": {}, "nas": {"unread": "timed out"}}, NAS_SETUP, ["vms"], now=NAS_NOW)
    assert unread["exit"] == 2


def test_the_nas_text():
    state = nas_state(
        pools=[
            pool("persistent", 2.2, 4.9),
            pool("ephemeral", 2.9, 0.7, layout="stripe", scan=scan(end=None, state="SCANNING", percentage=45.2)),
        ],
        alerts=[
            {
                "klass": "NTP",
                "level": "WARNING",
                "text": "NTP health check failed - No Active NTP peers" + " and more" * 9,
            },
            {"klass": "Update", "level": "INFO", "text": "An update is available"},
        ],
        tasks=[
            task("persistent/proxmox/vm", HOURLY),
            task("persistent/proxmox/vm", DAILY, last_run="2026-10-20T00:00:02+00:00"),
            task("persistent/nextcloud", HOURLY, state="ERROR", error="out of space"),
            task("persistent/nextcloud", WEEKLY, enabled=False),
            task("persistent/photoprism", HOURLY, state="PENDING", last_run=None),
        ],
    )
    out = fs.text(nas_report(state), False)
    lines = out.splitlines()
    assert max(len(line) for line in lines) <= 100, out
    assert "NAS  truenas · TrueNAS 25.10.7 · up 34 d" in out
    persistent = next(line for line in lines if " persistent " in line and "%" in line)
    assert persistent.startswith(" Pools      persistent  raidz1  online   ████░░░░░░░░  31%  2.2 / 7.1 TiB  "), out
    assert persistent.endswith("scrub 9 d ago, 0 errors"), out
    assert "ephemeral   stripe  online   ██████████░░  81%  2.9 / 3.6 TiB  scrubbing 45%" in out
    assert " Snapshots  proxmox/vm hourly, daily 59 m ago · nextcloud hourly, weekly (off) failed" in out
    assert "photoprism hourly not run yet" in out
    assert " NFS        running · 3 shares" in out
    alert = next(i for i, line in enumerate(lines) if line.startswith(" Alerts     warning   NTP health check"))
    assert lines[alert + 1].startswith(" " * 22) and lines[alert + 2].startswith("            info      An update")
    assert nas_report()["nas"]["alerts"] == [] and " Alerts     none" in fs.text(nas_report(), False)


def test_a_notice_shows_its_first_sentence_and_a_warning_its_whole_text():
    # A pool's feature-flags notice as TrueNAS 25.10.7 words it, seen on a real NAS.
    advice = (
        " Upgrading pools is a one-time process that can prevent rolling the system back to an earlier TrueNAS"
        " version. It is recommended to read the TrueNAS release notes and confirm you need the new ZFS feature flags"
        " before upgrading a pool."
    )
    notice = "New ZFS version or feature flags are available for pool 'persistent'."
    degraded = (
        "Pool persistent state is DEGRADED: One or more devices are faulted. The following devices are not healthy"
    )
    state = nas_state(
        alerts=[
            {"klass": "PoolUpgraded", "level": "NOTICE", "text": notice + advice},
            {"klass": "VolumeStatus", "level": "WARNING", "text": degraded},
        ]
    )
    model = nas_report(state)
    lines = fs.text(model, False).splitlines()
    # The notice on one line, then the warning, whole, wrapped over two.
    alerts = lines.index(" Alerts     notice    " + notice)
    assert lines[alerts + 1].startswith("            warning   Pool persistent state is DEGRADED")
    assert lines[alerts + 2] == " " * 22 + "following devices are not healthy"
    assert not any("Upgrading pools" in line for line in lines)
    assert model["nas"]["alerts"][0]["text"] == notice + advice, "the JSON keeps the whole text"


def test_the_nas_report_is_json():
    model = nas_report()
    assert json.loads(json.dumps(model)) == model
    assert "SERIAL-MUST-NOT-LEAK" not in json.dumps(model)
