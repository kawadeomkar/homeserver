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
        "made_by": "just deploy kubernetes",
        "start": "just resume --all",
        "node": "homeserver-cp-1",
    },
    {
        "vmid": 301,
        "name": "homeserver-worker-1",
        "made_by": "just deploy kubernetes",
        "start": "just resume --all",
        "node": "homeserver-worker-1",
    },
    {
        "vmid": 310,
        "name": "homeassistant",
        "made_by": "just deploy homeassistant",
        "start": "just resume --only homeassistant",
        "guest_agent": True,
        "app": {"name": "homeassistant", "url": "", "port": 8123},
    },
]
COMMANDS = {
    "upgrade": "just deploy proxmox",
    "resume": "just resume --only {names}",
    "talos_upgrade": "just talos-upgrade --check, then just talos-upgrade",
    "credentials": "just deploy kubernetes",
    "allowlist": "the first visits, then just deploy kubernetes",
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
        "rrd-9001": ok(history(lambda t: 0.3)),
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


def test_this_setups_vms_and_every_other():
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
    # Every other VM on the host is shown as Proxmox sees it: a template by its name, a running VM with its use, its
    # day's history and its boot disk.
    template, other = found()["others"]
    assert template == {"vmid": 9000, "name": "ubuntu-template", "status": "template"}
    assert other["vmid"] == 9001 and other["status"] == "running" and other["cores"] == 4
    assert other["memory"] == {"used": GIB, "total": 8 * GIB} and other["cpu_24h"] == [0.3] * 12
    assert other["boot_disk"] == 32 * GIB and "pending" not in other and "made_by" not in other
    # A VM on another node of a Proxmox cluster is that node's to show.
    elsewhere = vm_entry(9002, "on-another-node") | {"node": "pve2"}
    assert [vm["vmid"] for vm in found(resources=ok([*RESOURCES, elsewhere]))["others"]] == [9000, 9001]


def test_whose_history_is_read():
    # Every running VM's: this setup's and any other, never a template's or a stopped one's, nor another node's.
    elsewhere = vm_entry(9002, "on-another-node") | {"node": "pve2"}
    stopped = vm_entry(9003, "stopped-one", status="stopped")
    read = {"resources": ok([*RESOURCES, elsewhere, stopped])}
    assert fs.running_vms(read, "pve") == [300, 301, 310, 9001]
    assert fs.running_vms({}, "pve") == [] and fs.running_vms({"resources": failed("no")}, "pve") == []
    # Or, read at once, qemu-server's run directory: a <vmid>.pid for each VM running on this node, among the other
    # files a Proxmox VE 9.2 host keeps there (seen on the home host, 2026-11-17), whatever /cluster/resources says.
    listing = "\n".join(
        [f"{vmid}.{kind}" for vmid in (300, 301, 310, 9001) for kind in ("cleanup", "pid", "qga", "qmp", "serial0")]
        + ["310.vnc", "efidisk", "host-hw-capabilities.json", "pci-id-reservations", "pci-id-reservations.lock"]
    )
    assert fs.running_vms({"running": ok(listing + "\n"), **read}, "pve") == [300, 301, 310, 9001]
    assert fs.running_vms({"running": ok("")}, "pve") == []
    # The listing wins whatever /cluster/resources says: 9003 runs by its pid file though resources call it stopped,
    # and 301, which resources call running, has none.
    other = "\n".join(["300.pid", "310.pid", "9003.pid", "9003.qmp"])
    assert fs.running_vms({"running": ok(other), **read}, "pve") == [300, 310, 9003]
    # A stray file whose number is no VMID (100 up, nine digits at most) is not one: its history read would be off
    # status_read's read list, which refuses the whole pass.
    assert fs.running_vms({"running": ok("12.pid\n1234567890.pid\n0310.pid\n310.pid")}, "pve") == [310]
    # A listing that failed reads no other VM's history, and says so among the host's errors.
    assert fs.running_vms({"running": failed("ls: cannot access: No such file or directory", rc=2)}, "pve") == []
    errors = found(running=failed("ls: cannot access: No such file or directory", rc=2))["errors"]
    assert "which VMs run: ls: cannot access: No such file or directory" in errors
    assert not any(e.startswith("which VMs run") for e in found(running=ok(listing))["errors"])


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
    assert model["attention"][1]["command"] == "just deploy proxmox"
    assert model["attention"][2]["text"] == "301 homeserver-worker-1 waits for a restart to apply memory, watchdog"
    assert model["attention"][2]["command"] == "qm reboot 301"
    assert model["exit"] == 0 and model["types"] == [
        "admin",
        "vms",
        "nas",
        "storage",
        "cluster",
        "apps",
        "tailscale",
        "backups",
    ]
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
    assert items["stopped"]["command"] == "just resume --all"
    assert items["missing"]["text"] == "VM 310 homeassistant is not on this host"
    assert items["missing"]["command"] == "just deploy homeassistant"


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
    assert fs.types_shown([]) == ["admin", "vms", "nas", "storage", "cluster", "apps", "tailscale", "backups"]
    assert fs.types_shown(["storage", "vms"]) == ["vms", "storage"]


def test_the_report_is_json():
    model = fs.report({"proxmox": found()}, SETUP, [], "2026-10-21 14:40 PDT")
    assert json.loads(json.dumps(model)) == model


def test_each_source_is_said_by_the_host_that_reads_it():
    """SOURCE_HOSTS, whose findings the live callback waits for, and HOST_SOURCES, what each host reads, must agree: a
    source moved in one alone (as this machine's tailnet DNS moved to the cluster's) would leave the callback waiting
    for the wrong host. The probes are every host's own, read in its play beside its findings."""
    for source in fs.SOURCES:
        if source == "probes":
            continue
        readers = tuple(host for host, reads in fs.HOST_SOURCES.items() if source in reads)
        assert fs.SOURCE_HOSTS[source] == readers, source


def test_each_host_reads_only_what_the_sections_shown_need():
    every = ["proxmox", "tailscale", "tailnet_dns", "cluster", "etcd_snapshots", "probes", "nas"]
    assert fs.sources([]) == every
    assert fs.sources(["nas"]) == ["nas"]
    assert fs.sources(["admin"]) == ["tailscale"]
    assert fs.sources(["backups"]) == ["proxmox", "etcd_snapshots"]
    # The cluster's TLS line comes from the probes, which ask only when the apps are shown.
    assert fs.sources(["cluster"]) == ["proxmox", "cluster"]
    assert fs.sources(["apps"]) == ["proxmox", "cluster", "probes"]
    assert fs.sources(["apps", "tailscale"]) == ["proxmox", "tailscale", "tailnet_dns", "cluster", "probes"]
    assert fs.host_reads("proxmox", ["nas"]) == [] and fs.host_reads("cluster", ["nas"]) == []
    assert fs.host_reads("proxmox", ["admin"]) == ["tailscale"]
    assert fs.host_reads("cluster", ["backups"]) == ["etcd_snapshots"]
    assert fs.host_reads("nas", []) == ["nas"] and fs.host_reads("", []) == []
    # This machine's view of the tailnet's DNS is read with the cluster, the one source whose apps need it, so the
    # Proxmox host's own reads never wait for it.
    assert fs.host_reads("cluster", ["tailscale"]) == ["cluster", "tailnet_dns"]
    assert fs.host_reads("proxmox", ["tailscale"]) == ["tailscale"]
    assert fs.host_reads("cluster", ["apps"]) == ["cluster"]


def test_a_source_no_section_shown_needs_is_not_missed():
    """just status nas reads neither the Proxmox host nor the cluster, so neither is a source not read."""
    setup = {"nas": True, "cluster": True, "home_assistant": True}
    assert fs.report({}, setup, ["nas"])["attention"] == [
        {"level": "unread", "subject": "nas", "text": "the NAS was not read", "command": ""}
    ]
    assert [a["subject"] for a in fs.report({}, setup, ["admin"])["attention"]] == []
    assert [a["subject"] for a in fs.report({}, setup, ["vms"])["attention"]] == ["proxmox", "cluster"]


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
    # What needs attention comes after the sections it is worked out from, last, above the exit line.
    lines = out.splitlines()
    attention = lines.index("ATTENTION  0 problems · 3 to do")
    assert attention > next(i for i, line in enumerate(lines) if line.startswith("STORAGE"))
    assert lines[attention - 1] == "" and lines[-2] == "" and lines[-1].startswith("exit 0 · ")
    assert "→ qm reboot 301" in out and "→ just deploy proxmox" in out
    assert "VMS  pve · Proxmox VE 9.2.21 · kernel 7.0.14-22-pve · up 13 d" in out
    assert "/mnt/data 4.9 / 30" in out and "Home Assistant OS 18.3" in out
    # Every other VM follows this setup's, a diamond for its dot.
    assert " 9000  ◇ ubuntu-template      template" in out
    assert " 9001  ◆ another-project      5 h 30 m  13%  ▃▃▃▃▃▃▃▃▃▃▃▃   1.0 / 8.0   boot disk — / 32" in out
    assert " ◆ ◇ not this repo's: another project's, or made by hand; shown, never a problem or a thing to do" in out
    assert "the host's view: ballooning is off" in out
    assert "disks 68 of 68 GiB budgeted" in out
    assert out.rstrip().endswith(
        "exit 0 · nothing is wrong · sections: admin vms nas storage cluster apps tailscale backups · --json"
    )


def test_other_vms_follow_this_setups_and_line_up():
    others = [vm_entry(vmid, f"vm-{vmid}", cores=4, maxmem=16) for vmid in (9002, 9003)]
    long = vm_entry(100, "a-testing-vm-with-a-long-name", status="stopped")
    lines = report_text(resources=ok(RESOURCES + others + [long])).splitlines()
    assert max(len(line) for line in lines) <= 100, "\n".join(lines)
    rows = [line for line in lines if re.match(r" +\d+  [●○◆◇] ", line)]
    assert [int(line.split()[0]) for line in rows] == [300, 301, 310, 100, 9000, 9001, 9002, 9003]
    # A name too long for its column is cut, and the columns stay where the header puts them.
    header = next(line for line in lines if line.lstrip().startswith("VMID"))
    assert "  100  ◇ a-testing-vm-with-a-long…  stopped" in lines
    vm = next(line for line in rows if "vm-9002" in line)
    assert vm[header.index("UP") :].startswith("5 h 30 m") and vm[header.index("DISKS") :].startswith("boot disk")
    # None of them is ever a thing to do, stopped or not.
    out = "\n".join(lines)
    assert "ATTENTION  0 problems · 3 to do" in out and "a-testing-vm" not in out.split("ATTENTION")[1]


def test_the_other_vms_colour_says_they_are_not_this_repos():
    coloured = report_text(color=True)
    assert "\033[1mhomeserver-cp-1\033[0m" in coloured and "\033[36manother-project\033[0m" in coloured
    assert "\033[32m◆\033[0m \033[36manother-project" in coloured
    assert "\033[90m◇\033[0m \033[36mubuntu-template" in coloured


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
        "exit 2 · 1 source not read · sections: admin vms nas storage cluster apps tailscale backups · --json"
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
        # As k8s/apps/frigate's: WebRTC on one port over TCP and UDP.
        (("rtsp", 8554), ("webrtc-tcp", 8555), ("webrtc-udp", 8555)),
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
    assert apps["frigate"]["streams"] == ["rtsp://192.0.2.41:8554", "192.0.2.41:8555"]
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
    # In the sections' order: the VMs', then the apps'.
    assert subjects == ["reboot", "upgrades", "restart", "suspended"], model["attention"]
    suspended = model["attention"][3]
    assert suspended["text"] == f"sonarr is suspended since {iso(SUSPENDED_AT)[:10]}"
    assert suspended["command"] == "just resume --only sonarr" and model["exit"] == 0


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
    second = next(line for line in lines if line.split()[:1] == ["http://jellyfin.homeserver.internal"])
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
    "nas": "just deploy truenas",
    "nas_release": "raise truenas_supported_version",
    "nas_network": "commit them on the NAS",
    "nas_clock": "its NTP servers",
    "nas_disk_heat": "cool the disk",
    "nas_disk_errors": "the disks' SMART results",
    "nas_clients": "who holds the address",
    "time_machine": "look on the Mac",
    "time_machine_quota": "raise quota_gib",
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


# Each pool's disks, by the device names disk.temperatures gives them, and the boot pool's disk, which pool.query
# leaves out and disk.details names. Each disk's reading and critical temperature as TrueNAS reads them from the
# kernel's sensors: nvme2n1's 36.5 shows as 37, a half rounded up, and each critical temperature as the next whole
# degree.
POOL_DISKS = {"persistent": ["nvme1n1", "nvme2n1", "nvme3n1"], "ephemeral": ["sda"], "boot-pool": ["nvme0n1"]}
TEMPERATURES = {
    "nvme0n1": (45.0, 80.85),
    "nvme1n1": (36.0, 80.85),
    "nvme2n1": (36.5, 80.85),
    "nvme3n1": (38.0, 80.85),
    "sda": (30.0, 70.85),
}
SERIAL = "SERIAL-MUST-NOT-LEAK"
MODEL = "MODEL-MUST-NOT-LEAK"
# py-libzfs's words for a pool with a disk ZFS counts errors on (FAILING_DEV), which stays ONLINE though not healthy,
# and for one with a faulted disk (FAULTED_DEV_R), DEGRADED.
FAILING_DEV = (
    "One or more devices has experienced an unrecoverable error. An attempt was made to correct the error. "
    "Applications are unaffected."
)
FAULTED_DEV = (
    "One or more devices are faulted in response to persistent errors. Sufficient replicas exist for the pool to "
    "continue functioning in a degraded state."
)
# And for one with a disk gone (MISSING_DEV_R).
MISSING_DEV = (
    "One or more devices could not be opened. Sufficient replicas exist for the pool to continue functioning in a "
    "degraded state."
)


def disk(name, status="ONLINE", read=0, write=0, checksum=0, gone=False):
    """A pool's disk as truenas_info's status reads it from pool.query's topology."""
    counts = {"read_errors": read, "write_errors": write, "checksum_errors": checksum}
    return {"name": name, "status": status, "gone": gone, **counts}


def temperatures(**changed):
    """Every disk's reading and critical temperature, as truenas_info's status reads disk.temperatures."""
    readings = TEMPERATURES | changed
    return {name: {"temp": temp, "critical": critical} for name, (temp, critical) in readings.items()}


def disk_details():
    """disk.details' disks as truenas_info keeps them, each with the serial and model the report never shows."""
    return [
        {"name": name, "serial": SERIAL, "model": MODEL, "size": TIB, "imported_zpool": pool, "exported_zpool": None}
        for pool, names in POOL_DISKS.items()
        for name in names
    ]


def nfs_clients(*addresses):
    """The NFS clients as truenas_info's status reads them, each address once with how many clients it is: one, or
    the count given with it as (address, clients)."""
    return [
        {"address": a, "clients": 1} if isinstance(a, str) else {"address": a[0], "clients": a[1]} for a in addresses
    ]


def port(name, up=True, media="10000Mb/s Twisted Pair", mtu=1500, kind="PHYSICAL"):
    """An interface as truenas_info reads it from interface.query; a port that is down has no speed."""
    link = "LINK_STATE_UP" if up else "LINK_STATE_DOWN"
    return {"name": name, "aliases": [], "addresses": [], "link_state": link, "type": kind, "mtu": mtu, "media": media}


# Two ports, both up: one at 2.5 Gb/s, one at 1 Gb/s.
PORTS = [port("eno1", media="2500Mb/s Twisted Pair"), port("eno2", media="1000Mb/s Twisted Pair")]


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
        "disks": [disk(n) for n in POOL_DISKS.get(name, [])],
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


def backup(name="tm-laptop", used=400, quota=2048, snapshots=20, last_backup="2026-10-20T22:00:00+00:00", enabled=True):
    """A Time Machine share's backup as truenas_info's status reads it, sizes in GiB."""
    return {
        "name": name,
        "enabled": enabled,
        "dataset": f"persistent/timemachine/{name.removeprefix('tm-')}",
        "dataset_found": True,
        "used": used * 2**30,
        "quota": quota * 2**30,
        "snapshots_used": snapshots * 2**30,
        "last_backup": last_backup,
    }


def no_dataset(name="tm-laptop"):
    """A Time Machine share whose path names no dataset, as truenas_info's status reads it: no sizes, and no time."""
    sizes = {"used": None, "quota": None, "snapshots_used": None}
    return backup(name, last_backup=None) | {"dataset_found": False, **sizes}


def configured(*shares, retired=()):
    """The Macs the configuration has, as status_nas_macs gives them: each by its share, and whether it is retired."""
    return [{"share": share, "retired": share in retired} for share in shares]


def nas_state(pools=None, alerts=(), tasks=None, backups=(), health=None, **more):
    """What truenas_info returns with status, as roles/truenas's status entry point registers it: the ports, the disks
    (with their serials and models) and the shares beside the health, which `health` changes. The disks' temperatures
    and the NFS clients read, none of them, and the NAS's clock and this machine's alike."""
    return {
        "changed": False,
        "api_host": "192.0.2.10",
        "version": "25.10.7",
        "ready": True,
        "hostname": "truenas",
        "network_pending_changes": False,
        "interfaces": PORTS,
        "disks": disk_details(),
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
            "time_machine": list(backups),
            "datetime": iso(NAS_NOW),
            "read_at": iso(NAS_NOW),
            "temperatures": temperatures(),
            "nfs_clients": [],
            "unread": {},
            **(health or {}),
        },
        **more,
    }


# The NAS's services as group_vars/nas gives them (truenas_services, and cifs from truenas_smb_service), and the
# machines of this setup by address, as group_vars/all/status.yml names them.
NAS_SERVICES = {
    "nfs": {"enabled": True, "state": "started"},
    "ftp": {"enabled": False, "state": "stopped"},
    "ssh": {"enabled": False, "state": "stopped"},
    "cifs": {"enabled": False, "state": "stopped"},
}
NAS_MACHINES = {"192.0.2.20": "the Proxmox host", "192.0.2.30": "homeserver-cp-1", "192.0.2.31": "homeserver-worker-1"}


def nas_report(state=None, types=(), release="25.10", configured_macs=(), services=None, known=None, **found):
    """The report of the NAS beside a Proxmox host that holds nothing to report, with the Macs configured, and the
    services and machines of this setup when given."""
    state = nas_state() if state is None else state
    return fs.report(
        {"proxmox": {}, "nas": fs.nas(state, release, configured_macs, services, known), **found},
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
    assert found["nfs"] == {"enabled": True, "state": "RUNNING", "shares": 3, "clients": []}
    assert found["link"] == {"ports": 2, "up": 2, "speeds": ["2.5 Gb/s", "1 Gb/s"], "mtu": 1500}
    assert found["clock_drift"] == 0 and found["unexpected_services"] == [] and found["errors"] == []
    assert SERIAL not in json.dumps(found) and MODEL not in json.dumps(found)


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
            ("todo", "persistent", "no scrub task scrubs it", "just deploy truenas"),
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
                "just deploy truenas",
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
    # Not shown, the NAS is not read, so a NAS that would not answer is no source not read.
    unread = fs.report({"proxmox": {}, "nas": {"unread": "timed out"}}, NAS_SETUP, ["vms"], now=NAS_NOW)
    assert unread["exit"] == 0 and unread["attention"] == []
    assert fs.report({"nas": {"unread": "timed out"}}, NAS_SETUP, ["nas"], now=NAS_NOW)["exit"] == 2


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


def macs(*backups, smb="RUNNING"):
    """The NAS with Time Machine backups, each one's share, and the SMB service."""
    services = {"nfs": {"enable": True, "state": "RUNNING"}, "cifs": {"enable": smb == "RUNNING", "state": smb}}
    shares = [{"name": b["name"], "purpose": "TIMEMACHINE_SHARE", "enabled": b["enabled"]} for b in backups]
    return nas_state(backups=backups, services=services, smb_shares=shares)


# laptop, a Mac that backs up, as the configuration has it.
LAPTOP = configured("tm-laptop")


def test_a_backup_in_good_order_asks_for_nothing_and_shows_on_a_line():
    """A retired Mac is the configuration's: its share is disabled, and the module reads no time for it."""
    old = backup("tm-old", last_backup=None, enabled=False)
    model = nas_report(macs(backup(), old), configured_macs=configured("tm-laptop", "tm-old", retired=["tm-old"]))
    assert model["attention"] == [] and model["exit"] == 0
    found = model["nas"]
    assert found["smb"] == {"enabled": True, "state": "RUNNING", "shares": 1}
    keys = ("name", "configured", "retired", "share_found", "enabled", "dataset_found", "ago")
    assert [tuple(b[key] for key in keys) for b in found["time_machine"]] == [
        ("tm-laptop", True, False, True, True, True, 2 * 3600),
        ("tm-old", True, True, True, False, True, None),
    ]
    out = fs.text(model, False)
    assert " SMB        running · 1 share" in out, out
    assert " Macs       tm-laptop backed up 2 h ago, 0.4 / 2.0 TiB, snapshots 20 GiB · tm-old retired" in out, out


def test_without_smb_shares_the_report_has_no_smb_or_macs_line():
    out = fs.text(nas_report(), False)
    assert " SMB " not in out and " Macs " not in out


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            macs(backup(), smb="STOPPED"),
            ("problem", "smb", "the SMB service is stopped, so its 1 share is not served", "just deploy truenas"),
            id="smb-stopped",
        ),
        pytest.param(
            macs(backup(used=1700)),
            (
                "todo",
                "tm-laptop",
                "its Time Machine backup is 83% of its quota: 1.7 / 2.0 TiB, its snapshots 20 GiB",
                "raise quota_gib",
            ),
            id="quota-80",
        ),
        pytest.param(
            macs(backup(used=800, quota=1000)),
            (
                "todo",
                "tm-laptop",
                "its Time Machine backup is 80% of its quota: 800 / 1000 GiB, its snapshots 20 GiB",
                "raise quota_gib",
            ),
            id="quota-exactly-80",
        ),
        pytest.param(
            macs(backup(used=1900, snapshots=900)),
            (
                "problem",
                "tm-laptop",
                "its Time Machine backup is 93% of its quota: 1.9 / 2.0 TiB, its snapshots 900 GiB",
                "raise quota_gib",
            ),
            id="quota-90",
        ),
        pytest.param(
            macs(backup(used=900, quota=1000)),
            (
                "problem",
                "tm-laptop",
                "its Time Machine backup is 90% of its quota: 900 / 1000 GiB, its snapshots 20 GiB",
                "raise quota_gib",
            ),
            id="quota-exactly-90",
        ),
        pytest.param(
            macs(backup(last_backup="2026-10-10T00:00:00+00:00")),
            ("todo", "tm-laptop", "its newest Time Machine backup completed 11 d ago", "look on the Mac"),
            id="late",
        ),
        pytest.param(
            macs(backup(last_backup="2026-10-11T00:00:00+00:00")),
            ("todo", "tm-laptop", "its newest Time Machine backup completed 10 d ago", "look on the Mac"),
            id="late-exactly-10-days",
        ),
        pytest.param(
            macs(backup(last_backup=None)),
            ("todo", "tm-laptop", "no Time Machine backup to it has completed yet", "look on the Mac"),
            id="none-yet",
        ),
        pytest.param(
            macs(backup(enabled=False, last_backup=None)),
            (
                "problem",
                "tm-laptop",
                "its Time Machine share is disabled, so the Mac cannot back up",
                "just deploy truenas",
            ),
            id="share-disabled",
        ),
        pytest.param(
            macs(),
            ("todo", "tm-laptop", "the NAS has no Time Machine share of this name yet", "just deploy truenas"),
            id="no-share",
        ),
        pytest.param(
            macs(no_dataset()),
            (
                "problem",
                "tm-laptop",
                "its dataset persistent/timemachine/laptop is missing, so the Mac cannot back up",
                "just deploy truenas",
            ),
            id="no-dataset",
        ),
    ],
)
def test_what_a_backup_needs(state, expected):
    """laptop is a Mac the configuration has, not retired."""
    model = nas_report(state, configured_macs=LAPTOP)
    assert attention(model) == [expected]
    assert model["exit"] == (1 if expected[0] == "problem" else 0)


@pytest.mark.parametrize(
    ("given", "line"),
    [
        pytest.param(
            {"used": 799, "quota": 1000},
            "tm-laptop backed up 2 h ago, 799 / 1000 GiB, snapshots 20 GiB",
            id="quota-under-80",
        ),
        pytest.param(
            {"last_backup": "2026-10-11T01:00:00+00:00"},
            "tm-laptop backed up 9 d ago, 0.4 / 2.0 TiB, snapshots 20 GiB",
            id="9-days-23-hours",
        ),
        pytest.param({"quota": 0}, "tm-laptop backed up 2 h ago, 400 GiB, snapshots 20 GiB", id="no-quota"),
    ],
)
def test_a_backup_short_of_each_threshold_asks_for_nothing(given, line):
    model = nas_report(macs(backup(**given)), configured_macs=LAPTOP)
    assert model["attention"] == [] and model["exit"] == 0
    assert f" Macs       {line}" in fs.text(model, False)


@pytest.mark.parametrize(("used", "style"), [(400, ""), (1700, "33"), (1900, "31")])
def test_the_macs_line_colours_a_backup_as_the_pools_line_colours_a_pool(used, style):
    """Plain below 80% of its quota, yellow from it and red from 90%, as screen.fullness colours a pool's bar."""
    out = fs.text(nas_report(macs(backup(used=used)), configured_macs=LAPTOP), True)
    size = f"{used / 1024:.1f} / 2.0 TiB"
    assert (f"\033[{style}m{size}\033[0m" if style else f", {size}, snapshots 20 GiB") in out, out


def test_each_share_is_judged_by_the_macs_configured():
    """Retired is the configuration's word, never the share's: an active Mac's disabled share is a problem, its
    missing dataset another, and a Mac without a share yet a thing to do; a retired Mac's share, and a share no Mac
    names, nearly full and never backed up, are listed and ask for nothing. Configured Macs first, in their order."""
    state = macs(
        backup(),
        backup("tm-desk", enabled=False, last_backup=None),
        no_dataset("tm-gone"),
        backup("tm-old", enabled=False, last_backup=None),
        backup("tm-spare", used=2000, last_backup=None),
    )
    mine = configured("tm-laptop", "tm-desk", "tm-gone", "tm-studio", "tm-old", retired=["tm-old"])
    model = nas_report(state, configured_macs=mine)
    assert [(b["name"], b["configured"], b["retired"], b["share_found"]) for b in model["nas"]["time_machine"]] == [
        ("tm-laptop", True, False, True),
        ("tm-desk", True, False, True),
        ("tm-gone", True, False, True),
        ("tm-studio", True, False, False),
        ("tm-old", True, True, True),
        ("tm-spare", False, False, True),
    ]
    assert attention(model) == [
        ("problem", "tm-desk", "its Time Machine share is disabled, so the Mac cannot back up", "just deploy truenas"),
        (
            "problem",
            "tm-gone",
            "its dataset persistent/timemachine/gone is missing, so the Mac cannot back up",
            "just deploy truenas",
        ),
        ("todo", "tm-studio", "the NAS has no Time Machine share of this name yet", "just deploy truenas"),
    ]
    lines = fs.text(model, False).splitlines()
    first = lines.index(" Macs       tm-laptop backed up 2 h ago, 0.4 / 2.0 TiB, snapshots 20 GiB")
    assert lines[first + 1 : first + 3] == [
        "            tm-desk share disabled, 0.4 / 2.0 TiB, snapshots 20 GiB · tm-gone dataset missing",
        "            tm-studio no share yet · tm-old retired · tm-spare not configured",
    ]


@pytest.mark.parametrize("enabled", [False, True], ids=["share-disabled", "share-enabled"])
def test_a_retired_mac_is_never_late_or_full_whatever_its_share(enabled):
    """Neither late nor full, whatever its share holds; but a share still enabled is a thing to do, since the NAS play
    disables a retired Mac's."""
    retired = backup(used=2000, last_backup="2026-01-01T00:00:00+00:00", enabled=enabled)
    model = nas_report(macs(retired), configured_macs=configured("tm-laptop", retired=["tm-laptop"]))
    text = "the Mac is retired, but its Time Machine share is still enabled"
    assert attention(model) == ([("todo", "tm-laptop", text, "just deploy truenas")] if enabled else [])
    assert model["exit"] == 0
    lines = fs.text(model, False).splitlines()
    assert f" Macs       tm-laptop retired{', share enabled' if enabled else ''}" in lines, lines


def test_the_nas_report_is_json():
    model = nas_report()
    assert json.loads(json.dumps(model)) == model
    assert "SERIAL-MUST-NOT-LEAK" not in json.dumps(model)


def nas_block(model, color=False):
    """The NAS section's lines as the report shows them."""
    return dict(fs.blocks(model, color))["nas"]


def test_the_nas_network_and_disks_lines():
    """The ports up and their speed, then each pool's disks under the pools, the boot pool's after them, each with its
    reading in whole degrees (nvme2n1's 36.5 shows as 37), and who has the NFS shares mounted: nobody here."""
    lines = nas_block(nas_report())
    assert lines[1] == " Network    2.5 Gb/s · 1 Gb/s · 2 of 2 ports up"
    disks = lines.index(" Disks °C   persistent  nvme1n1 36 · nvme2n1 37 · nvme3n1 38")
    assert lines[disks - 2].startswith(" Pools      persistent  raidz1  online")
    assert lines[disks + 1 : disks + 3] == ["            ephemeral   sda 30", "            boot-pool   nvme0n1 45"]
    assert lines[disks + 3].startswith(" Snapshots  ")
    assert " NFS        running · 3 shares · mounted by nothing" in lines


def test_each_disk_by_pool_with_its_temperature_and_no_serial():
    """Each pool's disks in its topology's order, the boot pool's from disk.details, then a disk in no pool, here one
    TrueNAS has no reading for; neither serials nor models."""
    state = nas_state(health={"temperatures": temperatures(sdb=(None, None))})
    state["disks"].append({"name": "sdb", "serial": SERIAL, "model": MODEL, "imported_zpool": None})
    found = fs.nas(state, "25.10")
    assert [(d["name"], d["pool"], d["temp"], d["critical"]) for d in found["disks"]] == [
        ("nvme1n1", "persistent", 36, 81),
        ("nvme2n1", "persistent", 37, 81),
        ("nvme3n1", "persistent", 38, 81),
        ("sda", "ephemeral", 30, 71),
        ("nvme0n1", "boot-pool", 45, 81),
        ("sdb", "", None, None),
    ]
    assert found["disks"][0] == {
        "name": "nvme1n1",
        "pool": "persistent",
        "temp": 36,
        "critical": 81,
        "status": "ONLINE",
        "gone": False,
        "read_errors": 0,
        "write_errors": 0,
        "checksum_errors": 0,
    }
    assert found["temperatures_read"] is True
    assert SERIAL not in json.dumps(found) and MODEL not in json.dumps(found)
    lines = nas_block(nas_report(state))
    assert "            no pool     sdb —" in lines, lines


@pytest.mark.parametrize(
    ("value", "whole"),
    [(36.5, 37), (33.5, 34), (31.49, 31), (80.85, 81), (70, 70), (None, None), (True, None), ("40", None)],
)
def test_temperatures_show_in_whole_degrees_a_half_rounded_up(value, whole):
    assert fs.whole_celsius(value) == whole


@pytest.mark.parametrize(
    ("reading", "todo", "style"),
    [
        pytest.param(71.0, True, "33", id="exactly-10-below"),
        pytest.param(70.5, True, "33", id="70.5-shows-as-71"),
        pytest.param(70.49, False, "", id="11-below"),
        pytest.param(80.0, True, "33", id="1-below"),
        pytest.param(80.85, False, "31", id="at-critical"),
        pytest.param(90.0, False, "31", id="above-critical"),
    ],
)
def test_a_disk_near_its_critical_temperature(reading, todo, style):
    """Within 10 °C of the disk's own critical temperature, in the whole degrees shown, is a thing to do and yellow; at
    or above it red and nothing more, since TrueNAS's own alert says so."""
    model = nas_report(nas_state(health={"temperatures": temperatures(nvme3n1=(reading, 80.85))}))
    shown = fs.whole_celsius(reading)
    text = f"the disk is at {shown} °C, within 10 °C of its critical temperature of 81 °C"
    assert attention(model) == ([("todo", "nvme3n1", text, "cool the disk")] if todo else [])
    assert model["exit"] == 0
    line = next(line for line in nas_block(model, True) if "nvme3n1" in line)
    assert line.endswith(f"nvme3n1\033[{style}m {shown}\033[0m" if style else f"nvme3n1 {shown}"), line


@pytest.mark.parametrize("critical", [None, 0, 0.0, -273.15], ids=["none", "zero", "zero-float", "negative"])
def test_a_disk_without_a_critical_temperature_is_never_hot(critical):
    """No critical temperature, or one of 0 or below, which TrueNAS's own alert takes for none: never hot and never
    red, however warm the disk reads."""
    model = nas_report(nas_state(health={"temperatures": temperatures(nvme3n1=(90.0, critical), sda=(None, 70.85))}))
    assert model["attention"] == [] and model["nas"]["disks"][2]["critical"] is None
    lines = nas_block(model)
    assert " Disks °C   persistent  nvme1n1 36 · nvme2n1 37 · nvme3n1 90" in lines
    assert "            ephemeral   sda —" in lines
    assert "\033[31m" not in next(line for line in nas_block(model, True) if "nvme3n1" in line)


def test_a_disks_errors_and_state_beside_its_temperature():
    """ZFS's error counts on a disk and a state other than ONLINE are on its line, and its pool's problem names each
    disk with errors and its counts, never an item of its own: ZFS calls a pool with any of them not healthy, here
    DEGRADED by a faulted disk. The line wraps between disks, under the first."""
    persistent = [disk("nvme1n1"), disk("nvme2n1", read=1, write=2, checksum=3), disk("nvme3n1", "FAULTED", checksum=1)]
    degraded = pool("persistent", 2.2, 4.9, disks=persistent, status="DEGRADED", healthy=False, detail=FAULTED_DEV)
    state = nas_state(
        pools=[degraded, pool("ephemeral", 1.0, 2.6, layout="stripe")],
        health={"temperatures": temperatures(nvme1n1=(None, None))},
    )
    model = nas_report(state)
    errors = "nvme2n1 (1 read, 2 write, 3 checksum) and nvme3n1 (faulted, 1 checksum)"
    text = f"the pool is DEGRADED, with errors on {errors}: {FAULTED_DEV}"
    assert attention(model) == [("problem", "persistent", text, "the disks' SMART results")]
    assert model["exit"] == 1
    lines = nas_block(model)
    disks = lines.index(" Disks °C   persistent  nvme1n1 — · nvme2n1 37 (1 read, 2 write, 3 checksum)")
    assert lines[disks + 1] == " " * 24 + "nvme3n1 38 (faulted, 1 checksum)"
    assert any(line.startswith(" Pools      persistent  raidz1  degraded") for line in lines)
    out = "\n".join(nas_block(model, True))
    assert "\033[33m (1 read, 2 write, 3 checksum)\033[0m" in out and "\033[31m (faulted, 1 checksum)\033[0m" in out
    assert model["nas"]["disks"][1] == {
        "name": "nvme2n1",
        "pool": "persistent",
        "temp": 37,
        "critical": 81,
        "status": "ONLINE",
        "gone": False,
        "read_errors": 1,
        "write_errors": 2,
        "checksum_errors": 3,
    }


def test_a_disks_errors_are_its_pools_problem():
    """Checksum errors ZFS repaired: the pool stays ONLINE but is not healthy, one problem naming the disk and its
    count beside TrueNAS's own words, the pool's state red and the count yellow on their lines."""
    persistent = [disk("nvme1n1"), disk("nvme2n1", checksum=3), disk("nvme3n1")]
    failing = pool("persistent", 2.2, 4.9, disks=persistent, healthy=False, detail=FAILING_DEV)
    model = nas_report(nas_state(pools=[failing, pool("ephemeral", 1.0, 2.6, layout="stripe")]))
    text = f"the pool is ONLINE, with errors on nvme2n1 (3 checksum): {FAILING_DEV}"
    assert attention(model) == [("problem", "persistent", text, "the disks' SMART results")]
    assert model["exit"] == 1 and model["counts"]["nas"] == {"problem": 1, "todo": 0}
    lines = nas_block(model, True)
    assert "\033[31monline\033[0m" in next(line for line in lines if "persistent" in line and "%" in line)
    assert "\033[33m (3 checksum)\033[0m" in next(line for line in lines if "nvme2n1" in line)


def test_a_gone_disk_hides_no_disk_of_its_name():
    """A disk that is gone goes by the name TrueNAS last knew at its place, which the kernel may since have given its
    replacement: both are listed, the gone one in red with no reading, and the reading is the replacement's."""
    persistent = [disk("nvme1n1"), disk("nvme2n1", "UNAVAIL", read=2, gone=True), disk("nvme2n1"), disk("nvme3n1")]
    degraded = pool("persistent", 2.2, 4.9, disks=persistent, status="DEGRADED", healthy=False, detail=MISSING_DEV)
    model = nas_report(nas_state(pools=[degraded, pool("ephemeral", 1.0, 2.6, layout="stripe")]))
    assert [(d["name"], d["gone"], d["temp"]) for d in model["nas"]["disks"][:4]] == [
        ("nvme1n1", False, 36),
        ("nvme2n1", True, None),
        ("nvme2n1", False, 37),
        ("nvme3n1", False, 38),
    ]
    lines = nas_block(model)
    assert " Disks °C   persistent  nvme1n1 36 · nvme2n1 (gone, unavail, 2 read) · nvme2n1 37 · nvme3n1 38" in lines
    assert "\033[31m (gone, unavail, 2 read)\033[0m" in "\n".join(nas_block(model, True))
    text = f"the pool is DEGRADED, with errors on nvme2n1 (gone, unavail, 2 read): {MISSING_DEV}"
    assert attention(model) == [("problem", "persistent", text, "the disks' SMART results")]


def test_temperatures_not_read_leave_the_disks_without_them():
    """disk.temperatures refused: the disks are listed without a reading, the reads that failed say so, and the NAS
    is read all the same."""
    why = "disk.temperatures: [EPERM] Not authorized"
    state = nas_state(health={"temperatures": None, "unread": {"temperatures": why}})
    model = nas_report(state)
    assert model["exit"] == 0 and model["attention"] == []
    assert model["nas"]["temperatures_read"] is False and model["nas"]["errors"] == [f"the disks' temperatures: {why}"]
    assert [d["temp"] for d in model["nas"]["disks"]] == [None] * 5
    lines = nas_block(model)
    disks = lines.index(" Disks      persistent  nvme1n1 · nvme2n1 · nvme3n1")
    assert lines[disks + 1 : disks + 3] == ["            ephemeral   sda", "            boot-pool   nvme0n1"]
    assert dict(fs.blocks(model))["errors"] == [f" not read: the disks' temperatures: {why}", ""]


@pytest.mark.parametrize(
    ("media", "speed"),
    [
        ("10000Mb/s Twisted Pair", "10 Gb/s"),
        ("1000baseT <full-duplex>", "1 Gb/s"),
        ("2500baseT <full-duplex>", "2.5 Gb/s"),
        ("100baseTX <full-duplex>", "100 Mb/s"),
        ("10Gbase-SR", "10 Gb/s"),
        ("Unknown Twisted Pair", "Unknown Twisted Pair"),
        ("", "speed unknown"),
    ],
)
def test_a_ports_speed_from_how_truenas_words_it(media, speed):
    assert fs.link_speed(media) == speed


@pytest.mark.parametrize(
    ("ports", "line"),
    [
        pytest.param(PORTS, " Network    2.5 Gb/s · 1 Gb/s · 2 of 2 ports up", id="both-up"),
        pytest.param(
            [
                port("eno1", mtu=9000),
                port("eno2", media="1000baseT <full-duplex>", mtu=9000),
                port("eno3", up=False),
                port("eno4", up=False, media=""),
                port("br0", kind="BRIDGE", media=""),
            ],
            " Network    10 Gb/s · 1 Gb/s · MTU 9000 · 2 of 4 ports up",
            id="jumbo-frames-and-a-bridge",
        ),
        pytest.param([port("eno1", up=False)], " Network    0 of 1 port up", id="none-up"),
        pytest.param([], None, id="no-interfaces"),
    ],
)
def test_the_network_line(ports, line):
    """The physical ports alone, each up one's speed, and an MTU other than 1500; display only, never judged."""
    model = nas_report(nas_state(interfaces=ports))
    assert model["attention"] == []
    lines = nas_block(model)
    assert (lines[1] == line) if line else not any(line.startswith(" Network") for line in lines), lines


@pytest.mark.parametrize(
    ("addresses", "known", "mounted", "unexpected"),
    [
        pytest.param([], NAS_MACHINES, "mounted by nothing", [], id="no-client"),
        pytest.param(
            ["192.0.2.31", "192.0.2.20"],
            NAS_MACHINES,
            "mounted by the Proxmox host · homeserver-worker-1",
            [],
            id="this-setups-machines",
        ),
        pytest.param(
            ["192.0.2.20", "192.0.2.99", "::ffff:192.0.2.30"],
            NAS_MACHINES,
            "mounted by the Proxmox host · homeserver-cp-1 · 192.0.2.99",
            ["192.0.2.99"],
            id="an-unknown-client",
        ),
        pytest.param(
            [("192.0.2.20", 2), "192.0.2.31"],
            NAS_MACHINES,
            "mounted by the Proxmox host (2 clients) · homeserver-worker-1",
            [],
            id="two-clients-behind-one-address",
        ),
        pytest.param(["192.0.2.99"], {}, "mounted by 192.0.2.99", [], id="no-machine-configured"),
    ],
)
def test_who_has_the_nfs_shares_mounted(addresses, known, mounted, unexpected):
    """Each client named by the machines of this setup, theirs first, with how many clients it is when more than one;
    any other address is a problem, unless the configuration names no machine at all."""
    model = nas_report(nas_state(health={"nfs_clients": nfs_clients(*addresses)}), known=known)
    assert f" NFS        running · 3 shares · {mounted}" in nas_block(model)
    assert model["nas"]["unexpected_clients"] == unexpected
    text = "an NFS share is mounted from {}, which is no machine of this setup"
    assert attention(model) == [
        ("problem", "nfs", text.format(address), "who holds the address") for address in unexpected
    ]
    assert model["exit"] == (1 if unexpected else 0)
    if unexpected:
        assert "\033[31m192.0.2.99\033[0m" in "\n".join(nas_block(model, True))
        assert model["nas"]["nfs"]["clients"] == [
            {"address": "192.0.2.20", "name": "the Proxmox host", "clients": 1},
            {"address": "192.0.2.30", "name": "homeserver-cp-1", "clients": 1},
            {"address": "192.0.2.99", "name": "", "clients": 1},
        ]


def test_the_nfs_line_wraps_between_clients():
    """Each client a phrase of its own, so that many wrap the NFS line between them, under its first phrase."""
    addresses = ["192.0.2.20", "192.0.2.31", *(f"198.51.100.{n}" for n in range(101, 106))]
    lines = nas_block(nas_report(nas_state(health={"nfs_clients": nfs_clients(*addresses)}), known=NAS_MACHINES))
    nfs = next(i for i, line in enumerate(lines) if line.startswith(" NFS "))
    assert lines[nfs : nfs + 2] == [
        " NFS        running · 3 shares · mounted by the Proxmox host · homeserver-worker-1 · 198.51.100.101",
        "            198.51.100.102 · 198.51.100.103 · 198.51.100.104 · 198.51.100.105",
    ], lines
    assert max(len(line) for line in lines) <= 100


def test_nfs_clients_not_read_are_not_shown():
    why = "nfs.get_nfs4_clients: [EPERM] Not authorized"
    model = nas_report(nas_state(health={"nfs_clients": None, "unread": {"nfs_clients": why}}), known=NAS_MACHINES)
    assert model["exit"] == 0 and model["attention"] == [] and model["nas"]["nfs"]["clients"] is None
    assert " NFS        running · 3 shares" in nas_block(model) and "mounted" not in "\n".join(nas_block(model))
    assert dict(fs.blocks(model))["errors"] == [f" not read: the NFS clients: {why}", ""]


@pytest.mark.parametrize(
    ("services", "configured", "unexpected"),
    [
        pytest.param({"ssh": "RUNNING"}, NAS_SERVICES, ["ssh"], id="ssh-running"),
        pytest.param({"ssh": "RUNNING", "cifs": "RUNNING"}, NAS_SERVICES, ["ssh", "cifs"], id="ssh-and-smb"),
        pytest.param({"ssh": "STOPPED"}, NAS_SERVICES, [], id="ssh-stopped"),
        pytest.param({"nfs": "RUNNING", "snmp": "RUNNING"}, NAS_SERVICES, [], id="started-or-not-configured"),
        pytest.param({"ssh": "RUNNING"}, None, [], id="no-configuration"),
    ],
)
def test_a_service_that_runs_though_configured_stopped(services, configured, unexpected):
    """The JSON keeps TrueNAS's ids, and the text says SMB for cifs, as the rest of the report does."""
    state = nas_state(services={name: {"enable": False, "state": s} for name, s in services.items()})
    model = nas_report(state, services=configured)
    assert model["nas"]["unexpected_services"] == unexpected
    names = [{"cifs": "SMB"}.get(name, name) for name in unexpected]
    text = "{} is running, though the configuration keeps it stopped"
    assert [a for a in attention(model) if a[1] == "nas"] == [
        ("problem", "nas", text.format(name), "just deploy truenas") for name in names
    ]
    lines = nas_block(model)
    services_line = [line for line in lines if line.startswith(" Services")]
    shown = " · ".join(f"{name} running, configured stopped" for name in names)
    assert services_line == ([f" Services   {shown}"] if unexpected else []), lines


@pytest.mark.parametrize(
    ("drift", "text"),
    [
        pytest.param(-95, "the NAS's clock is 95 s behind this machine's", id="95-behind"),
        pytest.param(-60, "the NAS's clock is 60 s behind this machine's", id="60-behind"),
        pytest.param(60, "the NAS's clock is 60 s ahead of this machine's", id="60-ahead"),
        pytest.param(3 * 3600, "the NAS's clock is 3 h ahead of this machine's", id="hours-ahead"),
        pytest.param(59, None, id="59-ahead"),
        pytest.param(-59, None, id="59-behind"),
        pytest.param(0, None, id="in-step"),
    ],
)
def test_the_nas_clock_against_this_machines(drift, text):
    """A drift of 60 s or more either way is a thing to do, pointing at the NAS's NTP servers; nothing shows below."""
    model = nas_report(nas_state(health={"datetime": iso(NAS_NOW + drift), "read_at": iso(NAS_NOW)}))
    assert model["nas"]["clock_drift"] == drift
    assert attention(model) == ([("todo", "nas", text, "its NTP servers")] if text else [])
    assert "clock" not in "\n".join(nas_block(model))


def test_the_nas_lines_in_order():
    """Network, pools, disks, snapshots, NFS, SMB, the Macs, the services that run though configured stopped, alerts."""
    state = macs(backup())
    state["services"]["ssh"] = {"enable": False, "state": "RUNNING"}
    configured_services = NAS_SERVICES | {"cifs": {"enabled": True, "state": "started"}}
    model = nas_report(state, configured_macs=LAPTOP, services=configured_services, known=NAS_MACHINES)
    lines = nas_block(model)
    keys = [line[:12].strip() for line in lines[1:] if line[:12].strip()]
    assert keys == ["Network", "Pools", "Disks °C", "Snapshots", "NFS", "SMB", "Macs", "Services", "Alerts"], lines
    assert max(len(line) for line in lines) <= 100


def rich_nas():
    """A NAS whose every new line has something to say: a hot disk, one with errors, which make its pool not
    healthy, a service running though configured stopped, a client that is no machine of this setup, and a clock
    behind this machine's."""
    persistent = [disk("nvme1n1"), disk("nvme2n1", checksum=3), disk("nvme3n1")]
    failing = pool("persistent", 2.2, 4.9, disks=persistent, healthy=False, detail=FAILING_DEV)
    state = nas_state(
        pools=[failing, pool("ephemeral", 1.0, 2.6, layout="stripe")],
        health={
            "temperatures": temperatures(nvme3n1=(74.0, 80.85)),
            "nfs_clients": nfs_clients("192.0.2.20", "192.0.2.99"),
            "datetime": iso(NAS_NOW - 95),
            "read_at": iso(NAS_NOW),
        },
        alerts=[{"klass": "SMART", "level": "WARNING", "text": "A disk wears out."}],
    )
    state["services"]["ssh"] = {"enable": False, "state": "RUNNING"}
    return fs.nas(state, "25.10", (), NAS_SERVICES, NAS_MACHINES)


def unread_nas():
    """A NAS whose temperatures and NFS clients were refused: the reads that failed instead of what they say."""
    why = {"temperatures": "disk.temperatures: [EPERM] x", "nfs_clients": "nfs.get_nfs3_clients: [EPERM] x"}
    state = nas_state(health={"temperatures": None, "nfs_clients": None, "unread": why})
    return fs.nas(state, "25.10", (), NAS_SERVICES, NAS_MACHINES)


@pytest.mark.parametrize("nas_found", [rich_nas(), unread_nas()], ids=["every-new-line", "reads-refused"])
@pytest.mark.parametrize("every", [(), fs.TYPES], ids=["no-types", "every-type"])
def test_just_status_shows_the_nas_as_just_status_nas_does(nas_found, every):
    """just status is the union of its sections: the NAS's block, every new line included, what it needs and the reads
    that failed are the same whether the NAS is shown alone or with every other section."""
    found = everything() | {"nas": nas_found}
    alone, alone_model = blocks_of(found, ["nas"])
    whole, whole_model = blocks_of(found, list(every))
    assert alone["nas"] == whole["nas"]
    items = alone_model["attention"]
    assert [a for a in whole_model["attention"] if a in items] == items
    assert alone_model["counts"]["nas"] == whole_model["counts"]["nas"]
    assert all(line in whole["attention"] for line in alone.get("attention", [])[1:])
    assert all(line in whole.get("errors", []) for line in alone.get("errors", []))
    if nas_found["unexpected_services"]:
        assert {a["subject"] for a in items} == {"nas", "nfs", "persistent", "nvme3n1"}
        assert alone_model["counts"]["nas"] == {"problem": 3, "todo": 3}
    else:
        assert len(alone["errors"]) == 3


# ------------------------------------------------------------------------------------------------ backups ---
# The shapes captured on 2026-10-21 from a Proxmox VE 9.2 host's nightly job: /cluster/backup (the listing, which
# alone carries next-run), the vzdump runs, a run's log, the backups on the job's storage, and schedule-analyze.

JOB = {"id": "vm-nightly", "schedule": "02:00", "storage": "homeserver-backup", "vmid": [300, 301, 310]}
DAY = 86400
NEXT_RUN = NOW + 4 * 3600
DUE = NEXT_RUN - DAY
BACKUP_COMMANDS = COMMANDS | {
    "backup": "just deploy proxmox",
    "backup_target": "vm_backup_storage in local.yml, then just deploy proxmox",
    "etcd_snapshot": "just etcd-snapshot",
}
NAMES = {300: "homeserver-cp-1", 301: "homeserver-worker-1", 310: "homeassistant"}


def hosted_job(**more):
    return {
        "compress": "zstd",
        "enabled": 1,
        "id": "vm-nightly",
        "mode": "snapshot",
        "next-run": NEXT_RUN,
        "notes-template": "{{guestname}}",
        "prune-backups": {"keep-daily": "7", "keep-weekly": "4"},
        "schedule": "02:00",
        "storage": "homeserver-backup",
        "type": "vzdump",
        "vmid": "300,301,310",
    } | more


def upid(start, vmid=""):
    return f"UPID:pve:002BE7DF:04A56412:{start:08X}:vzdump:{vmid}:root@pam:"


def run(start, status="OK", vmid=""):
    entry = {"id": str(vmid), "node": "pve", "pid": 2877407, "pstart": 77947922, "starttime": start}
    entry |= {"type": "vzdump", "upid": upid(start, vmid), "user": "root@pam"}
    return entry if status is None else entry | {"endtime": start + 170, "status": status}


def archive(vmid, ctime, gib=1.5):
    return {
        "content": "backup",
        "ctime": ctime,
        "format": "vma.zst",
        "notes": NAMES[vmid],
        "size": int(gib * GIB),
        "subtype": "qemu",
        "vmid": vmid,
        "volid": f"homeserver-backup:backup/vzdump-qemu-{vmid}-{ctime}.vma.zst",
    }


def log(*lines):
    return [{"n": n + 1, "t": t} for n, t in enumerate(lines)]


def run_log(failed=(), asked=(300, 301, 310)):
    lines = [f"INFO: starting new backup job: vzdump {' '.join(map(str, asked))} --mode snapshot --storage x"]
    for vmid in asked:
        lines += [f"INFO: Starting Backup of VM {vmid} (qemu)", f"INFO: VM Name: {NAMES[vmid]}"]
        if vmid in failed:
            lines += [f"ERROR: Backup of VM {vmid} failed - {failed[vmid]}", "INFO: Failed at 2026-10-21 02:01:11"]
        else:
            lines += [f"INFO: Finished Backup of VM {vmid} (00:00:10)"]
    return log(*lines, "INFO: Backup job finished with errors", "TASK ERROR: job errors")


FREEZE = "VM 301 qmp command 'guest-fsfreeze-freeze' failed - got timeout"


def backup_results(runs=None, archives=None, **more):
    """Two nightly runs, the day before's and the last, every VM backed up by both."""
    runs = [run(DUE + 1), run(DUE - DAY + 1)] if runs is None else runs
    if archives is None:
        archives = [archive(v, start + 60 * i) for start in (DUE - DAY + 1, DUE + 1) for i, v in enumerate(NAMES)]
    return {
        "backup-jobs": ok([hosted_job()]),
        "backup-tasks": ok(runs),
        "backups-homeserver-backup": ok(archives),
        "schedule-vm-nightly": ok([{"timestamp": NEXT_RUN, "utc": "x"}, {"timestamp": NEXT_RUN + DAY, "utc": "x"}]),
    } | more


def backed_up(jobs=(JOB,), resources=None, **more):
    extra = {"resources": ok(resources)} if resources else {}
    return fs.proxmox(
        {"unread": "", "node": "pve", "results": results(**extra, **backup_results(**more))},
        VMS,
        ["local-lvm"],
        16,
        "/run/reboot-required",
        list(jobs),
    )


SNAPSHOTS = {"dir": "~/.talos/homeserver/etcd", "files": ["etcd-20261018T032459Z.db", "etcd-20261020T035037Z.db"]}


def backup_report(prox=None, snapshots=SNAPSHOTS, types=("backups",)):
    found_ = {"proxmox": prox or backed_up()} | ({"etcd_snapshots": snapshots} if snapshots is not None else {})
    return fs.report(found_, SETUP, list(types), commands=BACKUP_COMMANDS, now=NOW)


def backup_attention(model):
    return [(a["level"], a["text"], a["command"]) for a in model["attention"]]


def test_the_backups_as_read():
    found_ = backed_up()["backups"]
    assert found_["jobs"] == [
        {
            "id": "vm-nightly",
            "on_host": True,
            "enabled": True,
            "schedule": "02:00",
            "storage": "homeserver-backup",
            "wanted_storage": "homeserver-backup",
            "keep": {"daily": 7, "weekly": 4},
            "next_run": NEXT_RUN,
            "period": DAY,
            "vmids": [300, 301, 310],
            "wanted_vmids": [300, 301, 310],
        }
    ]
    vms = {vm["vmid"]: vm for vm in found_["vms"]}
    assert vms[301] == {
        "vmid": 301,
        "name": "homeserver-worker-1",
        "status": "running",
        "job": "vm-nightly",
        "wanted": True,
        "read": True,
        "newest": DUE + 61,
        "size": int(1.5 * GIB),
        "kept": 2,
        "failure": None,
    }
    assert found_["running"] is None
    # Not asked for (sections without backups): no reads, no section.
    assert found()["backups"] is None


def test_retention_from_the_object_or_the_text():
    assert fs._keep({"keep-daily": "7", "keep-weekly": "4"}) == {"daily": 7, "weekly": 4}
    assert fs._keep("keep-last=3, keep-monthly=2,keep-foo=1") == {"last": 3, "monthly": 2}
    assert fs._keep("keep-all=1") == {"all": 1} and fs._keep(None) == {}


def test_a_healthy_backup_asks_for_nothing():
    model = backup_report()
    assert model["attention"] == [] and model["exit"] == 0
    assert [vm["state"] for vm in model["backups"]["vms"]] == ["ok", "ok", "ok"]
    assert model["backups"]["etcd"] == {
        "dir": "~/.talos/homeserver/etcd",
        "count": 2,
        "age": NOW - int(datetime(2026, 10, 20, 3, 50, 37, tzinfo=UTC).timestamp()),
        "command": "just etcd-snapshot",
    }
    # The raw reads are not in the report twice.
    assert "backups" not in model["proxmox"]


def test_only_the_newest_failed_runs_logs_are_read():
    runs = [run(DUE + 1, "job errors"), run(DUE - DAY + 1, "WARNINGS: 1"), run(DUE - 2 * DAY, None)]
    runs += [run(DUE - n * DAY, "job errors") for n in range(3, 8)] + [run(DUE - 9 * DAY, "job errors", vmid="bad")]
    assert fs.backup_failed_runs({"backup-tasks": ok(runs)}) == [upid(DUE + 1)] + [upid(DUE - n * DAY) for n in (3, 4)]
    assert fs.backup_failed_runs({}) == [] and fs.backup_failed_runs({"backup-tasks": failed("x")}) == []


def test_a_vm_whose_backup_failed():
    runs = [run(DUE + 1, "job errors"), run(DUE - DAY + 1)]
    archives = [archive(v, DUE - DAY + 1) for v in NAMES] + [archive(v, DUE + 1) for v in (300, 310)]
    prox = backed_up(runs=runs, archives=archives, **{f"log-{upid(DUE + 1)}": ok(run_log(failed={301: FREEZE}))})
    model = backup_report(prox)
    assert backup_attention(model) == [
        (
            "problem",
            f"the backup of 301 homeserver-worker-1 failed 19 h ago: {FREEZE}; the last good one is 43 h old",
            "",
        )
    ]
    assert model["exit"] == 1
    worker = next(vm for vm in model["backups"]["vms"] if vm["vmid"] == 301)
    assert worker["state"] == "failed" and worker["failure"] == {"time": DUE + 1, "reason": FREEZE}


def test_a_run_that_failed_before_any_vm():
    """A storage that cannot be activated fails the run before its first VM: every VM it was to back up failed."""
    error = "could not activate storage 'homeserver-backup': mount error: mount.nfs: Connection timed out"
    runs = [run(DUE + 1, error), run(DUE - DAY + 1)]
    archives = [archive(v, DUE - DAY + 1) for v in NAMES]
    prox = backed_up(runs=runs, archives=archives, **{f"log-{upid(DUE + 1)}": ok(log(f"TASK ERROR: {error}"))})
    assert backup_attention(backup_report(prox)) == [
        (
            "problem",
            "the backups of 300 homeserver-cp-1, 301 homeserver-worker-1 and 310 homeassistant failed 19 h ago: "
            + error,
            "",
        )
    ]


def test_a_failure_since_made_good_is_no_failure():
    """A backup made after the failed run, by hand or by a later run, ends the failure."""
    runs = [run(DUE + 1, "job errors"), run(DUE - DAY + 1)]
    archives = [archive(v, DUE + 1) for v in (300, 310)] + [archive(301, DUE + 3600)]
    prox = backed_up(runs=runs, archives=archives, **{f"log-{upid(DUE + 1)}": ok(run_log(failed={301: FREEZE}))})
    assert backup_report(prox)["attention"] == []


def test_a_run_that_never_happened():
    """The host was off when the job was due: no run, no failure, every VM's newest backup older than the run due."""
    archives = [archive(v, DUE - DAY + 1) for v in NAMES]
    model = backup_report(backed_up(runs=[run(DUE - DAY + 1)], archives=archives))
    assert backup_attention(model) == [
        (
            "problem",
            "vm-nightly's run due 20 h ago made no backup of 300 homeserver-cp-1, 301 homeserver-worker-1 and 310 "
            "homeassistant",
            "",
        )
    ]
    # While a run goes on, the VMs it has not reached yet are not missed.
    running = backup_report(backed_up(runs=[run(NOW - 60, None), run(DUE - DAY + 1)], archives=archives))
    assert running["attention"] == [] and running["backups"]["running"] == 60


def test_a_daylight_saving_change_is_no_missed_run():
    """Between the next two runs the clocks change: the period worked out is 23 h, so the run due seems an hour
    later than it was."""
    schedule = ok([{"timestamp": NEXT_RUN, "utc": "x"}, {"timestamp": NEXT_RUN + DAY - 3600, "utc": "x"}])
    model = backup_report(backed_up(**{"schedule-vm-nightly": schedule}))
    assert model["attention"] == []


def test_a_vm_the_job_leaves_out_or_has_not_reached_yet():
    # A VM added to local.yml's extra VMIDs since the job was applied: left out until just deploy proxmox.
    left_out = backed_up(**{"backup-jobs": ok([hosted_job(vmid="300,301")])})
    assert backup_attention(backup_report(left_out)) == [
        ("todo", "the backup job vm-nightly leaves out 310 homeassistant", "just deploy proxmox")
    ]
    assert next(vm for vm in left_out["backups"]["vms"] if vm["vmid"] == 310)["job"] == ""
    # A VM with no backup yet, the job not run since it was added.
    fresh = backed_up(archives=[archive(v, DUE + 1) for v in (300, 301)])
    assert backup_attention(backup_report(fresh)) == [
        ("todo", "310 homeassistant has no backup yet: vm-nightly runs next in 4 h 0 m", "")
    ]
    # A VM that is not on the host says so in its own item, never as a backup.
    gone = backed_up(resources=[r for r in RESOURCES if r.get("vmid") != 310])
    model = backup_report(gone)
    assert backup_attention(model) == [] and model["backups"]["vms"][2]["state"] == "missing"


@pytest.mark.parametrize(
    ("hosted", "text"),
    [
        ([], "the backup job vm-nightly is not on the host"),
        ([hosted_job(enabled=0)], "the backup job vm-nightly is disabled"),
        ([hosted_job(storage="local")], "the backup job vm-nightly writes to local, not homeserver-backup"),
    ],
)
def test_a_job_that_differs_from_its_configuration(hosted, text):
    model = backup_report(backed_up(**{"backup-jobs": ok(hosted)}))
    assert backup_attention(model)[0] == ("todo", text, "just deploy proxmox")
    assert model["exit"] == 0


def test_nothing_backed_up_without_a_job():
    """Without a NAS and without vm_backup_storage there is no job: a thing to do, with where backups could go."""
    model = backup_report(backed_up(jobs=()))
    assert backup_attention(model) == [
        (
            "todo",
            "nothing is backed up: no backup job covers 300 homeserver-cp-1, 301 homeserver-worker-1 and 310 "
            "homeassistant",
            "vm_backup_storage in local.yml, then just deploy proxmox",
        )
    ]
    assert model["exit"] == 0


def test_backups_not_read_say_so_and_judge_nothing():
    model = backup_report(backed_up(**{"backup-jobs": failed("500 permission denied")}))
    assert model["attention"] == [] and "the backup jobs: 500 permission denied" in model["proxmox"]["errors"]
    assert {vm["state"] for vm in model["backups"]["vms"]} == {"unknown"}
    model = backup_report(backed_up(**{"backups-homeserver-backup": failed("storage is not online")}))
    assert model["attention"] == [] and {vm["state"] for vm in model["backups"]["vms"]} == {"unknown"}


def test_the_backups_text():
    runs = [run(DUE + 1, "job errors"), run(DUE - DAY + 1)]
    archives = [archive(v, DUE - DAY + 1, gib=9.4) for v in NAMES] + [archive(v, DUE + 1) for v in (300, 310)]
    prox = backed_up(runs=runs, archives=archives, **{f"log-{upid(DUE + 1)}": ok(run_log(failed={301: FREEZE}))})
    out = fs.text(backup_report(prox), False)
    lines = out.splitlines()
    assert max(len(line) for line in lines) <= 100, out
    assert "BACKUPS  vm-nightly → homeserver-backup · 02:00 · keeps 7 daily, 4 weekly · next in 4 h 0 m" in lines
    assert "  300  homeserver-cp-1       19 h ago  ok        1.5 GiB · 2 kept" in lines, out
    failure = lines.index(
        "  301  homeserver-worker-1   19 h ago  failed    VM 301 qmp command 'guest-fsfreeze-freeze' failed -"
    )
    assert lines[failure + 1] == " " * 49 + "got timeout", out
    assert lines[failure + 2] == " " * 29 + "43 h ago  ok        9.4 GiB · 1 kept", out
    assert "       etcd snapshot         28 h ago            2 in ~/.talos/homeserver/etcd" in lines, out


def test_the_text_without_a_job_or_a_snapshot():
    out = fs.text(backup_report(backed_up(jobs=()), snapshots={"dir": "~/.talos/pve-2/etcd", "files": []}), False)
    assert "BACKUPS  no backup job: 300, 301 and 310 are not backed up" in out
    assert "       etcd snapshot                —  none yet  in ~/.talos/pve-2/etcd  → just etcd-snapshot" in out
    assert "  300 " not in out
    # Not shown unless asked for, and nothing without a cluster's snapshots.
    assert "BACKUPS" not in fs.text(backup_report(types=("vms",)), False)
    assert "etcd snapshot" not in fs.text(backup_report(snapshots=None), False)


def test_the_backups_report_is_json():
    model = backup_report()
    assert json.loads(json.dumps(model)) == model


# ------------------------------------------------------------------------------------------ Tailscale ---

ROUTE = "192.0.2.0/24"
SUFFIX = "tail0example" + ".ts.net"
TAILNET_IPV4 = ".".join(["100", "64", "0", "20"])
TAILSCALE_COMMANDS = {
    "tailscale": "just deploy proxmox",
    "tailscale_login": "an auth key in the vault, then just deploy proxmox",
    "split_dns": "the admin console's DNS page",
}
NODE_PAGE = {"name": "Proxmox VE", "url": "https://192.0.2.20:8006", "tailscale_node": True}
ADMIN_PAGES = [
    NODE_PAGE,
    {"name": "TrueNAS", "url": "https://192.0.2.10"},
    {"name": "Kubernetes API", "url": "https://192.0.2.30:6443"},
    {"name": "Talos API", "url": "192.0.2.30:50000"},
]


def tail_state(routes=(ROUTE,), served=True, **more):
    """What roles/proxmox_tailscale's status entry point records for a subnet router that is as configured."""
    state = {
        "wanted_routes": list(routes),
        "wanted_tags": ["tag:homeserver"],
        "guard_hosts": ["192.0.2.10"],
        "guard_allow": ["tcp/443"],
        "installed": True,
        "running": True,
        "backend": "Running",
        "version": "1.104.2",
        "suffix": SUFFIX,
        "dns_name": f"pve.{SUFFIX}",
        "addresses": [TAILNET_IPV4],
        "tags": ["tag:homeserver"],
        "key_expiry": "",
        "routes": [{"route": r, "wanted": True, "advertised": True, "served": served} for r in routes],
        "prefs_read": True,
        "differ": [],
        "settings": {"accept-routes": False, "accept-dns": False, "ssh": False, "auto-update": False},
        "snat": True,
        "serve": False,
        "health": [],
        "guard": {
            "loaded": True,
            "routes": list(routes),
            "guarded": ["192.0.2.10"],
            "allow": {"192.0.2.10": ["tcp/443"]},
        },
        "forwarding": bool(routes),
        "console": [],
    }
    return state | more


def tailnet_dns(routes=None, suffix=SUFFIX):
    """tailscale dns status --json on this machine, with the tailnet's split DNS routes."""
    data = {
        "TailscaleDNS": True,
        "CurrentTailnet": {"MagicDNSEnabled": True, "MagicDNSSuffix": suffix},
        "SplitDNSRoutes": {"ts.net.": [{"Addr": "192.0.2.53"}], **(routes or {})},
    }
    return {"rc": 0, "stdout": json.dumps(data), "stderr": ""}


def tail_report(state=None, types=(), dns=None, admin=ADMIN_PAGES, setup=FULL_SETUP):
    return fs.report(
        {"proxmox": found(), "cluster": cluster_found(), "tailscale": tail_state() if state is None else state},
        setup,
        types,
        probes=probes(),
        admin=admin,
        commands=TAILSCALE_COMMANDS,
        now=NOW,
        tailnet_dns=dns,
    )


def test_the_apps_through_a_route_this_host_serves():
    model = tail_report(dns=tailnet_dns({"homeserver.internal.": [{"Addr": "192.0.2.1"}]}))
    assert model["tailscale"]["reach"] == {
        "state": "served",
        "route": ROUTE,
        "source": "192.0.2.20",
        "allowed": True,
        "split_dns": ["192.0.2.1"],
        "domain": "homeserver.internal",
    }
    assert [a for a in model["attention"] if a["subject"] == "tailscale"] == []
    out = fs.text(model, False)
    # Under the apps: how they answer from the tailnet, wrapped between its phrases.
    reach = f" Tailnet    the same URLs, through {ROUTE} · clients arrive as 192.0.2.20, inside the allowlist\n"
    assert reach + "            split DNS sends homeserver.internal to 192.0.2.1\n" in out, out
    assert "ADMIN             LAN                       TAILNET\n" in out
    assert f" Proxmox VE       https://192.0.2.20:8006   https://pve.{SUFFIX}:8006\n" in out
    assert " TrueNAS          https://192.0.2.10        through the route, tcp/443 only\n" in out
    assert " Kubernetes API   https://192.0.2.30:6443   through the route\n" in out
    assert "TAILSCALE  subnet router on the Proxmox host · 1.104.2 · ● running" in out
    assert f" Node       pve.{SUFFIX} · {TAILNET_IPV4} · tag:homeserver · key expiry off" in out
    assert f" Routes     {ROUTE} advertised, served by this host" in out
    assert " Guard      loaded · guards 192.0.2.10 (tcp/443 only) · forwarding on" in out
    assert " Prefs      accept-routes off · accept-dns off · ssh off · auto-update off · as configured" in out
    assert " Health     no warnings" in out
    assert max(len(line) for line in out.splitlines()) <= 100, out


def test_a_parent_domain_or_none_in_the_tailnets_split_dns():
    parent = tail_report(dns=tailnet_dns({"internal.": [{"Addr": "192.0.2.1"}]}))
    assert parent["tailscale"]["reach"]["split_dns"] == ["192.0.2.1"]
    missing = tail_report(dns=tailnet_dns())
    assert missing["tailscale"]["reach"]["split_dns"] == []
    item = next(a for a in missing["attention"] if a["subject"] == "tailscale")
    assert item == {
        "level": "todo",
        "subject": "tailscale",
        "text": "the tailnet has no split DNS for homeserver.internal: away from home, its clients do not resolve the "
        "apps",
        "command": "the admin console's DNS page",
    }
    assert "no split DNS for homeserver.internal\n" in fs.text(missing, False)
    # This machine on another tailnet, or without Tailscale: nothing said either way.
    for dns in (tailnet_dns(suffix="other" + ".ts.net"), {"rc": None, "stdout": "", "error": "No such file"}, None):
        model = tail_report(dns=dns)
        assert model["tailscale"]["reach"]["split_dns"] is None
        assert [a for a in model["attention"] if a["subject"] == "tailscale"] == []


def test_the_apps_through_a_route_another_router_serves():
    console = [{"kind": "routes", "text": f"Advertised but not approved, or served by another subnet router: {ROUTE}."}]
    model = tail_report(tail_state(served=False, console=console))
    assert model["tailscale"]["reach"]["state"] == "not served"
    assert [(a["level"], a["text"], a["command"]) for a in model["attention"] if a["subject"] == "tailscale"] == [
        ("todo", console[0]["text"], "")
    ]
    out = fs.text(model, False)
    assert f" Tailnet    through {ROUTE}, which this host does not serve" in out
    assert " Kubernetes API   https://192.0.2.30:6443   route not served by this host" in out
    assert f" Routes     {ROUTE} advertised, not served by this host" in out


def test_tailnet_clients_outside_the_allowlist():
    cluster = cluster_found(
        settings=ok({**SETTINGS, "data": SETTINGS["data"] | {"INGRESS_ALLOWED_RANGE": "127.0.0.1/32"}})
    )
    model = fs.report(
        {"proxmox": found(), "cluster": cluster, "tailscale": tail_state()},
        FULL_SETUP,
        probes=probes(),
        admin=ADMIN_PAGES,
        commands=TAILSCALE_COMMANDS,
        now=NOW,
    )
    assert model["tailscale"]["reach"]["allowed"] is False
    assert "clients arrive as 192.0.2.20, outside the allowlist" in fs.text(model, False)


def test_a_host_on_the_tailnet_without_routes():
    guard = {"loaded": True, "routes": [], "guarded": ["192.0.2.10"], "allow": {"192.0.2.10": ["tcp/443"]}}
    model = tail_report(tail_state(routes=(), guard=guard))
    assert model["tailscale"]["reach"] == {"state": "no route"}
    assert [a for a in model["attention"] if a["subject"] == "tailscale"] == []
    out = fs.text(model, False)
    assert "TAILSCALE  a device of its own on the Proxmox host · 1.104.2 · ● running" in out
    assert " Routes     none, so nothing is forwarded from the tailnet" in out
    assert " Guard      loaded · guards 192.0.2.10 (tcp/443 only) · forwarding off" in out
    assert " Tailnet    no route is advertised, so from the tailnet only the Proxmox host answers" in out
    assert " Talos API        192.0.2.30:50000          not without a route" in out


@pytest.mark.parametrize(
    "state, expected",
    [
        (
            {"installed": False, "running": False, "why": "No such file"},
            [("todo", "Tailscale is not on the Proxmox host yet", "just deploy proxmox")],
        ),
        (
            {"running": False, "why": "failed to connect to local tailscaled"},
            [
                (
                    "problem",
                    "tailscaled does not answer on the Proxmox host: failed to connect to local tailscaled",
                    "just deploy proxmox",
                )
            ],
        ),
        (
            {"backend": "NeedsLogin"},
            [
                (
                    "todo",
                    "the Proxmox host is not logged in to the tailnet",
                    "an auth key in the vault, then just deploy proxmox",
                )
            ],
        ),
        (
            {"backend": "Stopped"},
            [("problem", "Tailscale is stopped on the Proxmox host (tailscale down)", "just deploy proxmox")],
        ),
        (
            {"guard": {"loaded": False}},
            [
                (
                    "problem",
                    "the tailnet guard's table is not loaded, so tailscaled runs unguarded",
                    "just deploy proxmox",
                )
            ],
        ),
        (
            {"guard": {"loaded": True, "routes": [ROUTE], "guarded": [], "allow": {}}},
            [
                (
                    "todo",
                    "the tailnet guard on the host is not the one this configuration describes",
                    "just deploy proxmox",
                )
            ],
        ),
        (
            {"forwarding": False},
            [("problem", f"the host forwards no IPv4, so {ROUTE} leads nowhere", "just deploy proxmox")],
        ),
        (
            {"differ": ["accept-dns"]},
            [("todo", "the host's Tailscale preferences differ: accept-dns", "just deploy proxmox")],
        ),
        (
            {
                "health": ["UDP GRO is off"],
                "console": [{"kind": "health", "text": "Tailscale reports: UDP GRO is off"}],
            },
            [],
        ),
    ],
    ids=[
        "not-installed",
        "not-running",
        "needs-login",
        "stopped",
        "guard-not-loaded",
        "guard-differs",
        "forwarding-off",
        "preferences-differ",
        "health-shown-only",
    ],
)
def test_what_needs_attention_about_tailscale(state, expected):
    model = tail_report(tail_state(**state))
    found_items = [(a["level"], a["text"], a["command"]) for a in model["attention"] if a["subject"] == "tailscale"]
    assert found_items == expected
    out = fs.text(model, False)
    assert max(len(line) for line in out.splitlines()) <= 100, out


def test_tailscale_that_does_not_answer_in_the_text():
    out = fs.text(tail_report(tail_state(running=False, why="failed to connect to local tailscaled")), False)
    assert "TAILSCALE  subnet router on the Proxmox host · ● not running" in out
    assert " Why        failed to connect to local tailscaled" in out
    # Without a running node, the admin pages have no tailnet column.
    assert "TAILNET" not in out and " Proxmox VE       https://192.0.2.20:8006\n" in out
    unread = fs.text(tail_report({"unread": "it did not answer"}), False)
    assert "TAILSCALE  not read: the Proxmox host did not answer" in unread


def test_no_tailscale_section_when_it_is_off():
    model = tail_report({})
    assert model["tailscale"] is None and "TAILSCALE" not in fs.text(model, False)
    assert all("tailnet" not in page for page in model["admin"])
    assert "TAILSCALE" not in fs.text(tail_report(types=["apps", "vms"]), False)


# ------------------------------------------------------------------------------------- what is shown when ---

EVERY_SETUP = {"nas": True, "cluster": True, "home_assistant": True}


def everything():
    """Every source's findings, as status_merge puts the hosts' together: the Proxmox host's and its Tailscale, this
    machine's view of the tailnet's DNS, the cluster's and its etcd snapshots, the probes, the NAS's."""
    return {
        "proxmox": backed_up(),
        "tailscale": tail_state(),
        "tailnet_dns": tailnet_dns({"homeserver.internal.": [{"Addr": "192.0.2.1"}]}),
        "cluster": cluster_found(),
        "etcd_snapshots": SNAPSHOTS,
        "probes": probes(),
        "nas": fs.nas(nas_state(alerts=[{"klass": "SMART", "level": "WARNING", "text": "A disk wears out."}]), "25.10"),
    }


def blocks_of(found, types=()):
    model = fs.report(
        found,
        EVERY_SETUP,
        types,
        "2026-10-21 14:32 PDT",
        admin=ADMIN_PAGES,
        commands=COMMANDS | TAILSCALE_COMMANDS,
        now=NOW,
    )
    return dict(fs.blocks(model)), model


def test_the_text_is_its_blocks_in_order():
    blocks, model = blocks_of(everything())
    sections = ["admin", "vms", "nas", "storage", "cluster", "apps", "tailscale", "backups"]
    assert list(blocks) == ["head", *sections, "attention", "exit"]
    assert fs.text(model) == "\n".join(line for lines in blocks.values() for line in lines) + "\n"
    assert fs.text(model, True) == "\n".join(line for _, lines in fs.blocks(model, True) for line in lines) + "\n"
    # Each section's problems and things to do, which the callback shows while the sections before it are read.
    counted = sum(n for counts in model["counts"].values() for n in counts.values())
    assert counted == len([a for a in model["attention"] if a["level"] != "unread"]) > 0
    assert model["counts"]["nas"] == {"problem": 0, "todo": 1} and list(model["counts"]) == sections


@pytest.mark.parametrize("types", [(), ("apps",), ("cluster",), ("vms", "storage"), ("admin", "tailscale"), ("nas",)])
def test_each_section_depends_only_on_its_sources(types):
    """A section is the same whatever the sources it is not worked out from say, read or not: just status's callback
    prints each as soon as its own are read, and the report play's text then has the same. A source no section shown
    needs is never read, so it is not there to begin with."""
    full = {k: v for k, v in everything().items() if k in fs.sources(types)}
    whole, model = blocks_of(full, types)
    for section in fs.types_shown(types):
        needed = fs.section_sources(section, types)
        for source in set(full) - set(needed):
            partial, counts = blocks_of({k: v for k, v in full.items() if k != source}, types)
            assert partial.get(section) == whole.get(section), (section, source)
            assert counts["counts"][section] == model["counts"][section], (section, source)
        alone, counts = blocks_of({k: v for k, v in full.items() if k in needed}, types)
        assert alone.get(section) == whole.get(section) and counts["counts"][section] == model["counts"][section]


def test_the_hosts_findings_put_together():
    one, other = {"url": "https://a.homeserver.internal"}, {"url": "http://192.0.2.32:8123"}
    merged = fs.merge(
        [{"cluster": {"apps": []}, "probes": [one]}, {}, None, {"proxmox": {"vms": []}, "probes": [other]}]
    )
    assert merged == {"cluster": {"apps": []}, "proxmox": {"vms": []}, "probes": [one, other]}
    assert fs.merge([]) == {"probes": []}
    # The report takes the probes and this machine's tailnet DNS from them, when it is not given its own.
    full = everything()
    assert fs.report(full, EVERY_SETUP, now=NOW) == fs.report(
        {k: v for k, v in full.items() if k not in ("probes", "tailnet_dns")},
        EVERY_SETUP,
        probes=full["probes"],
        tailnet_dns=full["tailnet_dns"],
        now=NOW,
    )


def test_the_sections_a_setup_can_have():
    nothing = {"nas": False, "cluster": False, "home_assistant": False}
    assert fs.sections_expected(nothing) == ["admin", "vms", "storage", "tailscale", "backups"]
    assert fs.sections_expected(nothing | {"home_assistant": True}, ["apps", "cluster"]) == ["apps"]
    assert fs.sections_expected(EVERY_SETUP, ["nas", "vms"]) == ["vms", "nas"]
