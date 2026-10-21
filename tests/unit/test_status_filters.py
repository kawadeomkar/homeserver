"""roles/status's filters: what the Proxmox host's reads say, the report and its exit code, and the report as text.

The shapes are those captured from a Proxmox VE 9.2 host on 2026-10-21, written out here with made-up names and
sizes: /cluster/resources (its node, VMs, a template and storages), the node's status, a day's history, pending
changes, Home Assistant OS's and Talos's guest agents, a storage's volumes, and apt-get's simulated upgrade.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "status" / "filter_plugins" / "status.py"
_spec = importlib.util.spec_from_file_location("status_filters", PLUGIN)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)

GIB = 2**30
NOW = 1792570320
ANSI = re.compile(r"\033\[[0-9;]*m")


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
    {"vmid": 300, "name": "homeserver-cp-1", "made_by": "make kubernetes", "start": "make up ALL=1"},
    {"vmid": 301, "name": "homeserver-worker-1", "made_by": "make kubernetes", "start": "make up ALL=1"},
    {
        "vmid": 310,
        "name": "homeassistant",
        "made_by": "make homeassistant",
        "start": "make up ONLY=homeassistant",
        "guest_agent": True,
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


# ------------------------------------------------------------------------------------------------- report ---

SETUP = {"nas": False, "cluster": True, "home_assistant": True}


def test_what_needs_attention_and_the_exit_code():
    model = fs.report({"proxmox": found()}, SETUP, [], "2026-10-21 14:40 PDT")
    subjects = [(a["level"], a["subject"]) for a in model["attention"]]
    assert subjects == [("todo", "reboot"), ("todo", "upgrades"), ("todo", "restart")]
    assert model["attention"][2]["text"] == "301 homeserver-worker-1 waits for a restart to apply memory, watchdog"
    assert model["attention"][2]["command"] == "qm reboot 301"
    assert model["exit"] == 0 and model["types"] == ["vms", "storage"]


def test_stopped_and_missing_vms_say_what_starts_or_makes_them():
    stopped = [
        vm_entry(300, "homeserver-cp-1", status="stopped"),
        vm_entry(301, "homeserver-worker-1", status="stopped"),
    ]
    model = fs.report({"proxmox": found(resources=ok(stopped + RESOURCES[3:]))}, SETUP)
    items = {a["subject"]: a for a in model["attention"]}
    assert items["stopped"]["text"] == "300 homeserver-cp-1 and 301 homeserver-worker-1 are stopped"
    assert items["stopped"]["command"] == "make up ALL=1"
    assert items["missing"]["text"] == "VM 310 homeassistant is not on this host"
    assert items["missing"]["command"] == "make homeassistant"


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
    assert fs.unknown_types(["vms", "apps", "nas"]) == ["apps", "nas"]
    assert fs.types_shown([]) == ["vms", "storage"] and fs.types_shown(["storage", "vms"]) == ["vms", "storage"]


def test_the_report_is_json():
    model = fs.report({"proxmox": found()}, SETUP, [], "2026-10-21 14:40 PDT")
    assert json.loads(json.dumps(model)) == model


# --------------------------------------------------------------------------------------------------- text ---


def report_text(color=False, **more):
    return fs.text(fs.report({"proxmox": found(**more)}, SETUP, [], "2026-10-21 14:40 PDT"), color)


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
    assert out.startswith("homeserver  2026-10-21 14:40 PDT · Proxmox only + cluster + Home Assistant\n")
    assert "ATTENTION  0 problems · 3 to do" in out
    assert "→ qm reboot 301" in out and "→ make proxmox" in out
    assert "VMS  pve · Proxmox VE 9.2.21 · kernel 7.0.14-22-pve · up 13 d" in out
    assert "/mnt/data 4.9 / 30" in out and "Home Assistant OS 18.3" in out
    assert "+ 1 other VM (9001), 4 cores, 8.0 GiB, 1 running and 1 template (9000), not this repo's" in out
    assert "the host's view: ballooning is off" in out
    assert "disks 68 of 68 GiB budgeted" in out
    assert out.rstrip().endswith("exit 0 · nothing is wrong · make status TYPE=vms|storage · JSON=1")


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
        "exit 2 · 1 source not read · make status TYPE=vms|storage · JSON=1"
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
