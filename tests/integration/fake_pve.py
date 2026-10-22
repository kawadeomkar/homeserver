"""Stand-ins for the Proxmox VE commands roles/proxmox_storage, roles/proxmox_apt, roles/proxmox_backup and
roles/proxmox_vm run, for the idempotence tests.

`pvesh get /storage`, `pvesh get /nodes/<node>/config`, `pvesh get /nodes/<node>/storage/<id>/status`,
`pvesm add nfs`, `pvesm set`, `pvenode config set`, `systemctl daemon-reload` (counted in the state's
`daemon_reloads`), `hostname -s`, `apt-get update` and `apt-get dist-upgrade`, backed by a JSON file named by
$FAKE_PVE_STATE.
Output follows Proxmox VE 9.2, as captured on a fresh install: `disable` appears only when set, every
`/storage` entry carries a `digest` of the whole configuration, `content` comes back in an order that
differs from one listing to the next (it is a hash's order on a real host, so the fake rotates it, and a
status answer shows the latest listing's order), every status answer carries `shared`, 1 only for an NFS
storage, and sizes, 0 for a storage that is not active, and adding an id that exists, or one Proxmox's id
rule rejects (pve-storage-id: two or more letters, digits, hyphens, underscores and dots, starting with a
letter and ending with a letter or digit, case not mattering), fails as `pvesm add` does. A storage named
in the state's `inactive` list answers `active: 0` while still enabled, as one whose mount or pool failed to
come up does, and one whose `nodes` leaves this node out answers `enabled: 0`, as Proxmox does for a storage
restricted to other cluster nodes. Nothing the role runs changes either. Anything else exits non-zero, so an
unexpected command fails the test. A state's `storage_total` gives a storage's size in bytes (64 GiB otherwise).

Backup jobs: `pvesh get|create /cluster/backup` and `pvesh get|set /cluster/backup/<id>`, from Proxmox VE 9's
API schema, and since 2026-10-21 checked against a host's: a job's prune settings come back as an object and its
enabled flag as a number (the role reads text as well), and only the listing carries `next-run`. Ids follow
Proxmox's pve-configid rule, and an option the schema lacks fails as it does there. The backup runs make status
reads are described under backup_run().

VMs: `qm create|set|config|status|start|shutdown` and `qm disk resize`, and pvesh's /nodes/<node>/qemu,
/nodes/<node>/lxc (the state's `containers`), .../qemu/<id>/config and .../pending, /nodes/<node>/status,
/nodes/<node>/storage, .../storage/<id>/content (filtered by --content and --vmid; as on Proxmox, files that belong
to no VMID, such as imports and snippets, match any --vmid, and a container's volumes are listed as rootdir when the
test seeds them so), .../download-url and /nodes/<node>/tasks/<upid>/status. Written from Proxmox VE 9's API schema
and documentation, not yet captured from a host: `qm create` refuses a VMID that exists, a disk on a storage that
does not exist or holds no images, and an ISO that is not there; it names a new disk as Proxmox does,
vm-<id>-disk-<n> on block storage and <id>/vm-<id>-disk-<n>.<format> on a file or NFS storage, n the next free
number, an EFI disk 4M; it stores tags lower-cased and sorted and a MAC in upper case, and makes up a MAC when none
is given. On a running VM, `qm set` leaves what Proxmox cannot change live (cores, CPU, memory, ballooning, the
watchdog, the agent, the boot order, PCI devices) pending until the next `qm start`, and the configuration pvesh
returns shows pending values applied, as Proxmox's does by default. Deleting a drive drops it from the boot order,
and adding it back does not restore it. `qm disk resize` grows only. A download that fails (the state's
`download_fails`) leaves no file and still exits 0, as pvesh may; only the task's status says it failed.
`start_fails` makes the next `qm start` fail once and `shutdown_hangs` every `qm shutdown` time out (the guest
never powers off); both refuse a VM already as asked, as qm does, and `qm start` also refuses a VM one of whose
drives, the CD-ROM included, names a volume that is not there, as Proxmox does when it activates them. A backup
job's VMIDs start at 100. The state's `content` holds each storage's volumes, which tests seed with other guests'
disks and old backups; `node_memory` the node's memory; `pci_mappings` and `usb_mappings` the resource
mappings that exist.

Images (as on Proxmox VE 9.2, read from its API code): `download-url --content import` takes an .ova, .qcow2, .raw or
.vmdk; with `--checksum` it needs `--checksum-algorithm`, and the state's `checksum_fails` makes the task fail as a
mismatch does, storing nothing. A disk given as `<storage>:0,import-from=<volume>` is imported from that volume, or
from a disk inside an OVA there (`<storage>:import/<name>.ova/<disk>`, its disk `ova_disk_size` bytes, 32 GiB
otherwise), at the source's size; any other size with import-from is refused, as Proxmox's special syntax requires.
A disk inside an OVA is extracted first onto the storage `qm create --import-working-storage` names, or the OVA's own,
which must hold images and have a path, or the creation is refused: the installer's `local` holds no images.
USB devices (usb0 to usb14) reach a running VM at once (hotplug). `qm guest cmd <id> network-get-interfaces` answers
for a running VM as the QEMU guest agent does, its NIC with the VM's MAC and the state's `guest_ip` (127.0.0.1
otherwise), beside the loopback and a Docker bridge; `agent_down` counts calls that fail first, as while it boots.

make status's reads (roles/status), in the shapes captured from a Proxmox VE 9.2 host on 2026-10-21:
`pvesh get /cluster/resources`, the node, every VM (a template among them, with `template: 1`) and every storage in
one list, a VM's use from the state's `usage` (cpu, mem and uptime by VMID; a running VM otherwise uses a tenth of
its CPU and half its memory, and a stopped one nothing); `/nodes/<node>/rrddata` and `.../qemu/<id>/rrddata` with
`--timeframe day --cf AVERAGE`, a point every ten minutes over the last day (a real host has one a minute), the
first one carrying only its time, as for a VM started since; `/nodes/<node>/status` with the version, the running
kernel, the uptime and the root filesystem besides the memory; and `qm guest cmd <id> get-fsinfo` and `get-osinfo`
for a running VM, from the state's `guest_fsinfo` and `guest_osinfo` by VMID, which answer as Talos's agent does
otherwise: no filesystem, and no OS name. The state file is replaced whole on every save, so that commands run at
the same time, as make status runs them, never read one half written.

apt-get reads the deb822 .sources files in $FAKE_PVE_SOURCES_DIR, the directory the role is given. `update`
fails with 401 Unauthorized, as the real one does, when an enterprise.proxmox.com repository is still
enabled, and otherwise fills the package lists from the no-subscription repository if one is enabled, or with
nothing. `dist-upgrade` upgrades what the lists offer; `-s` only reports it; without `-y` and
DEBIAN_FRONTEND=noninteractive it fails, as the real one would stop to ask. Its lines follow the real ones: a
simulation's `Inst <package> [<old>] (<new> ...)`, without the old version for a new package, and dpkg's
`Unpacking <package> (<new>) over (<old>) ...`, without `over` for a new one. With the state's `dpkg_fails` set,
dist-upgrade unpacks everything and then fails as dpkg does when a package's script fails, with apt's error and
exit status 100.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from datetime import time as clock
from pathlib import Path
from urllib.parse import urlsplit

# Proxmox names the node after the short hostname, which the fake `hostname -s` answers. Fixed rather than
# the controller's own: macOS renames itself when the network hands it a name, and a rename between the
# role's `hostname -s` and a later call would fail the run.
NODE = "pve"
# What a fresh install has and what the no-subscription repository offers, by package. `lists` is what apt
# knows: empty until an update has read a repository that carries these packages.
# The kernel comes as a meta package and a signed package named after the kernel release.
PACKAGES = {
    "installed": {
        "pve-manager": "9.2.2-1",
        "proxmox-kernel-7.0": "7.0.2-6",
        "proxmox-kernel-7.0.2-6-pve-signed": "7.0.2-6",
    },
    "repository": {
        "pve-manager": "9.2.3-1",
        "proxmox-kernel-7.0": "7.0.14-23",
        "proxmox-kernel-7.0.14-23-pve-signed": "7.0.14-23",
    },
    "lists": {},
}


def fresh_state():
    return {
        "storage": [
            {"storage": "local", "type": "dir", "path": "/var/lib/vz", "content": "backup,iso,vztmpl,import"},
            {
                "storage": "local-lvm",
                "type": "lvmthin",
                "thinpool": "data",
                "vgname": "pve",
                "content": "images,rootdir",
            },
        ],
        "node": {},
        "mutations": 0,
        "reads": 0,
        "inactive": [],
        "packages": json.loads(json.dumps(PACKAGES)),
        "apt_get_runs": 0,
    }


def state_path():
    return Path(os.environ["FAKE_PVE_STATE"])


def load():
    if not state_path().exists():
        return fresh_state()
    return json.loads(state_path().read_text())


def save(state):
    # Written aside and renamed over the state, so that a command reading it meanwhile sees it whole.
    path = state_path()
    scratch = path.with_name(f".{path.name}.{os.getpid()}")
    scratch.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(scratch, path)


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(255)


def options(args):
    """--key value pairs, plus --delete <names>."""
    out, i = {}, 0
    while i < len(args):
        if not args[i].startswith("--") or i + 1 >= len(args):
            fail(f"400 unable to parse option {args[i]!r}")
        out[args[i][2:]] = args[i + 1]
        i += 2
    return out


def rotated(content, reads):
    """A comma-separated list, rotated by the number of listings so far."""
    items = content.split(",")
    shift = reads % len(items)
    return ",".join(items[shift:] + items[:shift])


def listing(state):
    """The storage list as `pvesh get /storage` prints it, with `content` in a different order each time."""
    reads = state["reads"] = state.get("reads", 0) + 1
    digest = hashlib.sha1(json.dumps(state["storage"], sort_keys=True).encode()).hexdigest()
    return [{**e, "content": rotated(e["content"], reads), "digest": digest} for e in state["storage"]]


def pvesh(state, args):
    """pvesh <get|create|set> <path> [--option value ...] [--output-format json]: get prints JSON and needs the
    format; create and set print nothing, as for these paths on a real host."""
    if len(args) < 2 or args[0] not in ("get", "create", "set"):
        fail(f"fake pvesh: unsupported {args}")
    verb, path, rest = args[0], args[1], args[2:]
    formatted = rest[-2:] == ["--output-format", "json"]
    if formatted:
        rest = rest[:-2]
    if verb == "get" and not formatted:
        fail(f"fake pvesh: get needs --output-format json: {args}")
    for route, handler in ROUTES:
        found = route.fullmatch(path)
        if found and verb in handler:
            result = handler[verb](state, *found.groups(), options(rest))
            if verb == "get":
                print(json.dumps(result))
            return
    fail(f"fake pvesh: unsupported {verb} {path}")


def no_options(opts):
    if opts:
        fail(f"400 Parameter verification failed.\n{sorted(opts)[0]}: property is not defined in schema")


def get_storage(state, opts):
    no_options(opts)
    return listing(state)


def get_node_config(state, node, opts):
    # As on a real host, /nodes/localhost/config (or any name but the node's own) answers {},
    # and values come back as strings.
    return {k: str(v) for k, v in state["node"].items()} if node == NODE else {}


def check_node(node):
    if node not in (NODE, "localhost"):
        fail(f"500 hostname lookup '{node}' failed")


def get_storage_status(state, node, sid, opts):
    check_node(node)
    no_options(opts)
    entry = next((s for s in state["storage"] if s["storage"] == sid), None)
    if entry is None:
        fail(f"500 storage '{sid}' does not exist")
    restricted = "nodes" in entry and NODE not in entry["nodes"].split(",")
    enabled = not entry.get("disable") and not restricted
    active = enabled and sid not in state.get("inactive", [])
    total = state.get("storage_total", {}).get(sid, 64 * 2**30) if active else 0
    # content in the latest listing's order: counting status reads too would change the order the next
    # listing shows, which the content-order test depends on.
    return {
        "active": 1 if active else 0,
        "avail": total - total // 8,
        "content": rotated(entry["content"], state.get("reads", 0)),
        "enabled": 1 if enabled else 0,
        "shared": 1 if entry["type"] == "nfs" else 0,
        "total": total,
        "type": entry["type"],
        "used": total // 8,
    }


# A scheduled backup job's settings, as `pvesh create /cluster/backup` takes them (Proxmox VE 9's vzdump job
# schema, the ones this repo uses and a few more), and Proxmox's rule for its id (pve-configid).
BACKUP_OPTIONS = {
    "schedule",
    "storage",
    "vmid",
    "all",
    "exclude",
    "node",
    "mode",
    "compress",
    "prune-backups",
    "notes-template",
    "enabled",
    "comment",
    "mailto",
    "mailnotification",
}


def backup_job(state, opts):
    unknown = set(opts) - BACKUP_OPTIONS - {"delete"}
    if unknown:
        fail(f"400 Parameter verification failed.\n{sorted(unknown)[0]}: property is not defined in schema")
    if "mode" in opts and opts["mode"] not in ("snapshot", "suspend", "stop"):
        fail(
            f"400 Parameter verification failed.\nmode: value '{opts['mode']}' does not have a value in the enumeration"
        )
    if "prune-backups" in opts:
        for item in opts["prune-backups"].split(","):
            key, _, value = item.partition("=")
            if not key.startswith("keep-") or not value.isdigit():
                fail(f"400 Parameter verification failed.\nprune-backups: invalid format - {item}")
    if "vmid" in opts and (
        not re.fullmatch(r"\d+(,\d+)*", opts["vmid"]) or any(int(v) < 100 for v in opts["vmid"].split(","))
    ):
        fail("400 Parameter verification failed.\nvmid: value does not look like a valid VM ID list")


def shown_job(job):
    """A job as `pvesh get /cluster/backup` shows it: prune settings as an object, enabled as a number."""
    shown = {**job, "type": "vzdump", "enabled": int(job.get("enabled", "1"))}
    if "prune-backups" in job:
        shown["prune-backups"] = dict(item.split("=", 1) for item in job["prune-backups"].split(","))
    return shown


def get_backup_jobs(state, opts):
    """The jobs as the listing shows them, which alone carries each job's next run: the state's `next_runs` by id,
    else the next time a daily HH:MM schedule fires."""
    out = []
    for job in state.get("backup_jobs", {}).values():
        shown = shown_job(job)
        next_run = state.get("next_runs", {}).get(job["id"])
        if next_run is None and re.fullmatch(r"\d{1,2}:\d{2}", job.get("schedule", "")):
            next_run = daily(job["schedule"], time.time(), 1)[0]
        if next_run is not None:
            shown["next-run"] = int(next_run)
        out.append(shown)
    return out


def create_backup_job(state, opts):
    jid = opts.pop("id", None)
    if jid is None or not re.fullmatch(r"[a-z][a-z0-9_-]+", jid, re.IGNORECASE):
        fail(f"400 Parameter verification failed.\nid: invalid configuration ID '{jid}'")
    backup_job(state, opts)
    jobs = state.setdefault("backup_jobs", {})
    if jid in jobs:
        fail(f"500 Job '{jid}' already exists")
    jobs[jid] = {"id": jid, **opts}
    state["mutations"] += 1


def get_backup_job(state, jid, opts):
    job = state.get("backup_jobs", {}).get(jid)
    if job is None:
        fail(f"500 Job '{jid}' does not exist")
    return shown_job(job)


def set_backup_job(state, jid, opts):
    get_backup_job(state, jid, opts)
    backup_job(state, opts)
    job = state["backup_jobs"][jid]
    for name in opts.pop("delete", "").split(","):
        job.pop(name, None)
    job.update(opts)
    state["mutations"] += 1


# ------------------------------------------------------------------------------------------ backup runs ---
# make status reads when a schedule fires (/cluster/jobs/schedule-analyze), the backup runs (/nodes/<node>/tasks with
# --typefilter vzdump) and a run's log (/nodes/<node>/tasks/<upid>/log), in the shapes captured from a Proxmox VE 9.2
# host on 2026-10-21: a finished run's `status` is OK or its error, a running one has neither status nor end time,
# and a log is numbered lines that end with TASK OK or TASK ERROR. Runs come from the state's `tasks`, which
# backup_run() below seeds as vzdump would leave them; the fake runs no backup itself.


def daily(schedule, after, count):
    """The next `count` times, after `after`, that a daily HH:MM schedule fires in local time; no other calendar
    form is known here."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", schedule)
    if not match:
        fail(f"fake pvesh: only daily HH:MM schedules are known here, not {schedule!r}")
    day, out = datetime.fromtimestamp(after).date(), []
    while len(out) < count:
        when = datetime.combine(day, clock(int(match.group(1)), int(match.group(2)))).timestamp()
        if when > after:
            out.append(int(when))
        day += timedelta(days=1)
    return out


def schedule_analyze(state, opts):
    if set(opts) - {"schedule", "iterations", "starttime"} or "schedule" not in opts:
        fail(f"400 Parameter verification failed: {sorted(opts)}")
    times = daily(opts["schedule"], int(opts.get("starttime", time.time())), int(opts.get("iterations", 10)))
    return [{"timestamp": t, "utc": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(t))} for t in times]


def vzdump_upid(start, vmid=""):
    return f"UPID:{NODE}:{start % 2**24:08X}:{start % 2**28:08X}:{start:08X}:vzdump:{vmid}:root@pam:"


def backup_run(state, start, vmids, storage, failed=None, running=False, error=""):
    """Seed a backup run as vzdump leaves it: its task and log, and a backup on `storage` of each VM it made, a
    minute apart. `failed` maps the VMs it failed to why; `error` fails it before any VM, as a storage that cannot
    be activated does; `running` leaves it running, with no VM done yet."""
    failed, lines = (
        failed or {},
        [f"INFO: starting new backup job: vzdump {' '.join(map(str, vmids))} --storage {storage}"],
    )
    when = start
    for vmid in [] if error or running else vmids:
        name = state.get("vms", {}).get(str(vmid), {}).get("config", {}).get("name", "")
        lines += [f"INFO: Starting Backup of VM {vmid} (qemu)", f"INFO: VM Name: {name}"]
        if vmid in failed:
            lines += [f"ERROR: Backup of VM {vmid} failed - {failed[vmid]}", "INFO: Failed at"]
        else:
            stamp = time.strftime("%Y_%m_%d-%H_%M_%S", time.localtime(when))
            state.setdefault("content", {}).setdefault(storage, []).append(
                {
                    "content": "backup",
                    "ctime": when,
                    "format": "vma.zst",
                    "notes": name,
                    "size": (vmid % 7 + 1) * 2**30,
                    "subtype": "qemu",
                    "vmid": vmid,
                    "volid": f"{storage}:backup/vzdump-qemu-{vmid}-{stamp}.vma.zst",
                }
            )
            lines.append(f"INFO: Finished Backup of VM {vmid} (00:01:00)")
        when += 60
    task = {"type": "vzdump", "id": "", "user": "root@pam", "starttime": start, "log": lines}
    if error:
        task.update(endtime=start + 1, exitstatus=error)
    elif not running:
        lines.append("INFO: Backup job finished " + ("with errors" if failed else "successfully"))
        task.update(endtime=when, exitstatus="job errors" if failed else "OK")
    state.setdefault("tasks", {})[vzdump_upid(start)] = task


def shown_task(upid, task):
    out = {"upid": upid, "node": NODE, "pid": 1, "pstart": 1, "starttime": task["starttime"], "type": task["type"]}
    out |= {"id": task.get("id", ""), "user": task.get("user", "root@pam")}
    if "endtime" in task:
        out |= {"endtime": task["endtime"], "status": task["exitstatus"]}
    return out


def get_tasks(state, node, opts):
    check_node(node)
    if set(opts) - {"typefilter", "source", "limit", "start", "errors"}:
        fail(f"400 Parameter verification failed.\n{sorted(opts)[0]}: property is not defined in schema")
    source = opts.get("source", "archive")
    tasks = [
        shown_task(upid, t)
        for upid, t in state.get("tasks", {}).items()
        if "starttime" in t and opts.get("typefilter", t["type"]) == t["type"]
    ]
    if source != "all":
        tasks = [t for t in tasks if ("endtime" in t) == (source == "archive")]
    tasks.sort(key=lambda t: -t["starttime"])
    start = int(opts.get("start", 0))
    return tasks[start : start + int(opts.get("limit", 50))]


def get_task_log(state, node, upid, opts):
    check_node(node)
    if set(opts) - {"limit", "start"}:
        fail(f"400 Parameter verification failed.\n{sorted(opts)[0]}: property is not defined in schema")
    task = state.get("tasks", {}).get(upid)
    if task is None:
        fail(f"no such task '{upid}'")
    lines = list(task.get("log", []))
    if "endtime" in task:
        lines.append("TASK OK" if task["exitstatus"] == "OK" else f"TASK ERROR: {task['exitstatus']}")
    start = int(opts.get("start", 0))
    # As Proxmox does, 50 lines unless asked for more.
    return [{"n": n + 1, "t": t} for n, t in enumerate(lines)][start : start + int(opts.get("limit", 50))]


# ---------------------------------------------------------------------------------------------- VMs ---
# Settings Proxmox cannot change on a running VM with its default hotplug setting (network, disk, usb): they wait,
# pending, for the next start. The configuration read back shows them at once.
COLD = ("cores", "cpu", "memory", "balloon", "watchdog", "agent", "boot", "sockets", "machine", "bios", "scsihw", "vga")
VM_OPTIONS = {
    "name",
    "ostype",
    "machine",
    "bios",
    "efidisk0",
    "cpu",
    "sockets",
    "cores",
    "memory",
    "balloon",
    "scsihw",
    "net0",
    "agent",
    "serial0",
    "vga",
    "watchdog",
    "onboot",
    "startup",
    "ide2",
    "boot",
    "tags",
}
BLOCK_TYPES = ("lvmthin", "lvm", "zfspool")


def vm_option(name):
    return name in VM_OPTIONS or re.fullmatch(r"scsi([0-9]|[12][0-9]|30)|hostpci([0-9]|1[0-5])|usb([0-9]|1[0-4])", name)


def size_text(size):
    for unit, factor in (("T", 2**40), ("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if size % factor == 0:
            return f"{size // factor}{unit}"
    return str(size)


def parse_size(text):
    found = re.fullmatch(r"(\d+)([KMGT]?)", text)
    if not found:
        fail(f"400 Parameter verification failed.\nsize: unable to parse size '{text}'")
    return int(found.group(1)) * {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}[found.group(2)]


def storage_entry(state, sid):
    entry = next((s for s in state["storage"] if s["storage"] == sid), None)
    if entry is None:
        fail(f"storage '{sid}' does not exist")
    return entry


def new_disk(state, vmid, slot, value):
    """A disk given as <storage>:<size in GiB>[,...]: allocate a volume, as Proxmox names it on that storage, and
    return the disk as the configuration then shows it."""
    head, _, rest = value.partition(",")
    sid, _, size = head.partition(":")
    entry = storage_entry(state, sid)
    if "images" not in entry["content"].split(","):
        fail(f"storage '{sid}' does not support vm images")
    opts = dict(item.split("=", 1) for item in rest.split(",") if item)
    source = opts.pop("import-from", None)
    working = state.get("import_working_storage")
    if source is not None and ".ova/" in source:
        # A disk inside an OVA is extracted first, onto a storage that holds images and has a path.
        extraction = storage_entry(state, working or source.split(":", 1)[0])
        if "images" not in extraction["content"].split(",") or not extraction.get("path"):
            fail(
                f"400 Parameter verification failed.\n{'import-working-storage' if working else slot}: import working "
                f"storage '{extraction['storage']}' does not support 'images' content type or is not file based."
            )
    if source is not None:
        if size != "0":
            fail(
                f"400 Parameter verification failed.\n{slot}: 'import-from' requires special syntax - "
                "use <storage ID>:0,import-from=<source>"
            )
        size = str(import_size(state, source) // 2**30)
    content = state.setdefault("content", {}).setdefault(sid, [])
    taken = {v["volid"] for v in content}
    number = 0
    while any(f"vm-{vmid}-disk-{number}" in volid for volid in taken):
        number += 1
    if entry["type"] in BLOCK_TYPES:
        volid, fmt = f"{sid}:vm-{vmid}-disk-{number}", "raw"
    else:
        fmt = opts.pop("format", entry.get("format", "raw"))
        volid = f"{sid}:{vmid}/vm-{vmid}-disk-{number}.{fmt}"
    opts.pop("format", None)
    nbytes = 4 * 2**20 if slot == "efidisk0" else parse_size(f"{size}G")
    content.append({"volid": volid, "content": "images", "vmid": int(vmid), "size": nbytes, "format": fmt})
    return ",".join([volid, *(f"{k}={v}" for k, v in sorted(opts.items())), f"size={size_text(nbytes)}"])


def import_size(state, source):
    """The size of what a disk is imported from: a volume, or a disk inside an OVA (<storage>:import/<x>.ova/<disk>)."""
    found = re.fullmatch(r"([^:]+):import/([^/]+\.ova)/([^/]+\.(qcow2|raw|vmdk))", source)
    volid = f"{found.group(1)}:import/{found.group(2)}" if found else source
    entry = next((v for vols in state.get("content", {}).values() for v in vols if v["volid"] == volid), None)
    if entry is None:
        fail(f"volume '{volid}' does not exist")
    return state.get("ova_disk_size", 32 * 2**30) if found else entry["size"]


def tags_text(value):
    return ";".join(sorted({tag.strip().lower() for tag in re.split(r"[;, ]", value) if tag.strip()}))


def vm_of(state, vmid):
    vm = state.get("vms", {}).get(str(vmid))
    if vm is None:
        fail(f"Configuration file 'nodes/{NODE}/qemu-server/{vmid}.conf' does not exist")
    return vm


def mac_for(vmid, index=0):
    """A MAC Proxmox would generate: its own prefix, then bytes of its choosing."""
    return ":".join(["BC", "24", "11", f"{int(vmid) // 256 % 256:02X}", f"{int(vmid) % 256:02X}", f"{index:02X}"])


def checked_value(state, vmid, key, value):
    """An option's value as the configuration stores it."""
    if re.fullmatch(r"scsi\d+|efidisk0", key) and re.fullmatch(r"[^:,]+:\d+(,.*)?", value):
        return new_disk(state, vmid, key, value)
    if re.fullmatch(r"scsi\d+", key):
        # An existing volume with new options: Proxmox writes its size back itself.
        volid, *opts = value.split(",")
        volume = next((v for vols in state.get("content", {}).values() for v in vols if v["volid"] == volid), None)
        if volume is None:
            fail(f"volume '{volid}' does not exist")
        opts = [o for o in opts if not o.startswith("size=")]
        return ",".join([volid, *opts, f"size={size_text(volume['size'])}"])
    if key == "tags":
        return tags_text(value)
    if key == "ide2":
        volid = value.split(",", 1)[0]
        if volid not in ("none", "cdrom") and not any(
            v["volid"] == volid for vols in state.get("content", {}).values() for v in vols
        ):
            fail(f"volume '{volid}' does not exist")
    if key == "net0":
        model, _, rest = value.partition(",")
        name, _, mac = model.partition("=")
        return f"{name}={(mac or mac_for(vmid)).upper()},{rest}"
    if key.startswith("hostpci"):
        mapping = dict(item.split("=", 1) for item in value.split(",") if "=" in item).get("mapping")
        if mapping and mapping not in state.get("pci_mappings", []):
            fail(f"no such resource mapping '{mapping}'")
    if key.startswith("usb"):
        device = dict(item.split("=", 1) for item in value.split(",") if "=" in item)
        if not re.fullmatch(
            r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}|\d+-\d+(\.\d+)*", device.get("host", "")
        ) and not device.get("mapping"):
            fail(f"400 Parameter verification failed.\n{key}: invalid format - {value}")
        if device.get("mapping") and device["mapping"] not in state.get("usb_mappings", []):
            fail(f"no such resource mapping '{device['mapping']}'")
    return value


def qm(state, args):
    verb = args[0]
    if verb == "create":
        vmid, opts = args[1], options(args[2:])
        # A parameter of the command, not a VM option: where every disk the command imports from an OVA is extracted.
        state["import_working_storage"] = opts.pop("import-working-storage", None)
        if vmid in state.setdefault("vms", {}):
            fail(f"unable to create VM {vmid} - VM {vmid} already exists on node '{NODE}'")
        unknown = [name for name in opts if not vm_option(name) and name != "start"]
        if unknown:
            fail(f"400 Parameter verification failed.\n{unknown[0]}: property is not defined in schema")
        opts.pop("start", None)
        config = {key: checked_value(state, vmid, key, value) for key, value in opts.items()}
        state.pop("import_working_storage", None)
        state["vms"][vmid] = {"config": config, "pending": {}, "status": "stopped"}
    elif verb == "set":
        vmid, opts = args[1], options(args[2:])
        vm = vm_of(state, vmid)
        unknown = [name for name in opts if not vm_option(name) and name != "delete"]
        if unknown:
            fail(f"400 Parameter verification failed.\n{unknown[0]}: property is not defined in schema")
        running = vm["status"] == "running"
        for key in [k for k in opts.pop("delete", "").split(",") if k]:
            if running and (key in COLD or key.startswith("hostpci")):
                vm["pending"][key] = None
            else:
                vm["config"].pop(key, None)
                # Proxmox drops a deleted drive from the boot order, and adding one back never restores it.
                order = vm["config"].get("boot", "").removeprefix("order=").split(";")
                if key in order:
                    kept = [device for device in order if device != key]
                    vm["config"]["boot"] = "order=" + ";".join(kept)
        for key, value in opts.items():
            value = checked_value(state, vmid, key, value)
            if running and (key in COLD or key.startswith("hostpci")):
                vm["pending"][key] = value
            else:
                vm["config"][key] = value
    elif verb == "config":
        vm = vm_of(state, args[1])
        for key, value in sorted(applied(vm).items()):
            print(f"{key}: {value}")
        return
    elif verb == "status":
        print(f"status: {vm_of(state, args[1])['status']}")
        return
    elif (
        verb == "guest"
        and args[1:2] == ["cmd"]
        and args[3:]
        in (
            ["network-get-interfaces"],
            ["get-fsinfo"],
            ["get-osinfo"],
        )
    ):
        vm = vm_of(state, args[2])
        if vm["status"] != "running":
            fail(f"VM {args[2]} is not running")
        if state.get("agent_down", 0) > 0:
            state["agent_down"] -= 1
            save(state)
            fail("QEMU guest agent is not running")
        if args[3] == "get-fsinfo":
            print(json.dumps(state.get("guest_fsinfo", {}).get(args[2], []), indent=3))
            return
        if args[3] == "get-osinfo":
            print(json.dumps(state.get("guest_osinfo", {}).get(args[2], TALOS_OSINFO), indent=3))
            return
        # The loopback's and Docker's MACs and addresses are built here: gitleaks flags them written out.
        mac = configured_mac(applied(vm)).lower()
        print(
            json.dumps(
                [
                    {
                        "name": "lo",
                        "hardware-address": ":".join(["00"] * 6),
                        "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}],
                    },
                    {
                        "name": "enp0s18",
                        "hardware-address": mac,
                        "ip-addresses": [
                            {"ip-address-type": "ipv4", "ip-address": state.get("guest_ip", "127.0.0.1"), "prefix": 24},
                            {"ip-address-type": "ipv6", "ip-address": "fe80" + "::1", "prefix": 64},
                        ],
                    },
                    {
                        "name": "hassio",
                        "hardware-address": ":".join(["02", "42", "ac", "1e", "20", "01"]),
                        "ip-addresses": [
                            {"ip-address-type": "ipv4", "ip-address": ".".join(["172", "30", "32", "1"]), "prefix": 23}
                        ],
                    },
                ],
                indent=2,
            )
        )
        return
    elif verb == "start":
        vm = vm_of(state, args[1])
        if vm["status"] == "running":
            fail(f"VM {args[1]} already running")
        if state.get("start_fails"):
            state["start_fails"] = False
            save(state)
            fail(f"start failed: QEMU exited with code 1 (VM {args[1]})")
        # Proxmox activates every drive's volume before it starts a VM, the CD-ROM's too.
        volumes = {v["volid"] for vols in state.get("content", {}).values() for v in vols}
        for key, value in sorted(applied(vm).items()):
            volid = value.split(",", 1)[0]
            if re.fullmatch(r"(ide|sata|scsi|virtio)\d+|efidisk0", key) and ":" in volid and volid not in volumes:
                fail(f"volume '{volid}' does not exist")
        vm["config"] = applied(vm)
        vm["pending"] = {}
        vm["status"] = "running"
    elif verb == "shutdown":
        vm = vm_of(state, args[1])
        if args[2:] not in ([], ["--timeout", args[-1]]):
            fail(f"400 unable to parse option {args[2]}")
        if vm["status"] != "running":
            fail(f"VM {args[1]} not running")
        if state.get("shutdown_hangs"):
            fail(f"VM quit/powerdown failed - got timeout (VM {args[1]})")
        vm["status"] = "stopped"
    elif verb == "disk" and args[1:2] == ["resize"]:
        vmid, slot, size = args[2], args[3], args[4]
        vm = vm_of(state, vmid)
        if slot not in vm["config"]:
            fail(f"disk '{slot}' does not exist")
        volid, _, rest = vm["config"][slot].partition(",")
        opts = dict(item.split("=", 1) for item in rest.split(",") if item)
        current = parse_size(opts["size"])
        wanted = current + parse_size(size[1:]) if size.startswith("+") else parse_size(size)
        if wanted < current:
            fail(f"shrinking disks is not supported (VM {vmid}, {slot})")
        opts["size"] = size_text(wanted)
        vm["config"][slot] = ",".join([volid, *(f"{k}={v}" for k, v in sorted(opts.items()))])
        for volume in state["content"][volid.split(":")[0]]:
            if volume["volid"] == volid:
                volume["size"] = wanted
    else:
        fail(f"fake qm: unsupported {args}")
    state["mutations"] += 1


def configured_mac(config):
    model, _, _ = config.get("net0", "").partition(",")
    return model.partition("=")[2]


def applied(vm):
    """A VM's configuration with its pending changes applied, as Proxmox returns it by default."""
    config = dict(vm["config"])
    for key, value in vm["pending"].items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return config


# As pvesh returns a configuration: whole numbers as numbers, everything else as text.
NUMERIC = ("cores", "sockets", "balloon", "onboot")


def shown_config(config):
    return {k: int(v) if k in NUMERIC and str(v).isdigit() else v for k, v in config.items()}


def get_lxc(state, node, opts):
    """The node's containers, which tests seed in the state's `containers`."""
    check_node(node)
    no_options(opts)
    return [
        {"vmid": int(c["vmid"]), "name": c.get("name", ""), "status": c.get("status", "stopped")}
        for c in state.get("containers", [])
    ]


def get_qemu(state, node, opts):
    check_node(node)
    no_options(opts)
    return [
        {
            "vmid": int(vmid),
            "name": vm["config"].get("name", f"VM {vmid}"),
            "status": vm["status"],
            "maxmem": int(str(applied(vm).get("memory", "512")).split("=")[-1]) * 2**20,
        }
        for vmid, vm in sorted(state.get("vms", {}).items())
    ]


def get_vm_config(state, node, vmid, opts):
    check_node(node)
    no_options(opts)
    config = shown_config(applied(vm_of(state, vmid)))
    return {**config, "digest": hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()}


def get_vm_pending(state, node, vmid, opts):
    check_node(node)
    no_options(opts)
    vm = vm_of(state, vmid)
    out = []
    for key in sorted(set(vm["config"]) | set(vm["pending"])):
        entry = {"key": key}
        if key in vm["config"]:
            entry["value"] = shown_config({key: vm["config"][key]})[key]
        if key in vm["pending"]:
            if vm["pending"][key] is None:
                entry["delete"] = 1
            else:
                entry["pending"] = shown_config({key: vm["pending"][key]})[key]
        out.append(entry)
    return out


def get_node_status(state, node, opts):
    check_node(node)
    no_options(opts)
    memory = state.get("node_memory", {"total": 64 * 2**30, "used": 4 * 2**30})
    root = {"total": 96 * 2**30, "used": 12 * 2**30}
    return {
        "memory": {**memory, "free": memory["total"] - memory["used"]},
        "cpuinfo": {"cpus": 8},
        "cpu": 0.12,
        "uptime": NODE_UPTIME,
        "pveversion": f"pve-manager/{state.get('packages', PACKAGES)['installed']['pve-manager']}/4f6e0ac86f9e8c7f",
        "current-kernel": {"machine": "x86_64", "release": KERNEL, "sysname": "Linux"},
        "rootfs": {**root, "avail": root["total"] - root["used"], "free": root["total"] - root["used"]},
        "loadavg": ["0.67", "0.53", "0.62"],
    }


# What /cluster/resources and the history say of the host and its VMs, besides what the state holds.
NODE_UPTIME = 1141465
KERNEL = "7.0.2-6-pve"
TALOS_OSINFO = {"kernel-release": "6.18.54-talos", "kernel-version": "#1 SMP PREEMPT_DYNAMIC", "machine": "x86_64"}
NOW = 1792570320


def vm_usage(state, vmid, vm):
    """A VM's CPU and memory use and uptime: the state's `usage`, or a tenth of its CPU and half its memory."""
    memory = int(str(applied(vm).get("memory", "512")).split("=")[-1]) * 2**20
    if vm["status"] != "running":
        return {"cpu": 0, "mem": 0, "uptime": 0, "maxmem": memory}
    usage = {"cpu": 0.1, "mem": memory // 2, "uptime": 3600, **state.get("usage", {}).get(str(vmid), {})}
    return {**usage, "maxmem": memory}


def get_cluster_resources(state, opts):
    no_options(opts)
    memory = state.get("node_memory", {"total": 64 * 2**30, "used": 4 * 2**30})
    out = [
        {
            "id": f"node/{NODE}",
            "type": "node",
            "node": NODE,
            "status": "online",
            "cpu": 0.12,
            "maxcpu": 8,
            "mem": memory["used"],
            "maxmem": memory["total"],
            "disk": 12 * 2**30,
            "maxdisk": 96 * 2**30,
            "uptime": NODE_UPTIME,
        }
    ]
    for vmid, vm in sorted(state.get("vms", {}).items(), key=lambda item: int(item[0])):
        config = applied(vm)
        usage = vm_usage(state, vmid, vm)
        entry = {
            "id": f"qemu/{vmid}",
            "type": "qemu",
            "vmid": int(vmid),
            "node": NODE,
            "name": config.get("name", f"VM {vmid}"),
            "status": vm["status"],
            "template": int(config.get("template", 0)),
            "maxcpu": int(config.get("cores", 1)),
            "cpu": usage["cpu"],
            "mem": usage["mem"],
            "maxmem": usage["maxmem"],
            "disk": 0,
            "maxdisk": 0,
            "uptime": usage["uptime"],
        }
        if config.get("tags"):
            entry["tags"] = config["tags"]
        out.append(entry)
    for entry in state["storage"]:
        status = get_storage_status(state, NODE, entry["storage"], {})
        out.append(
            {
                "id": f"storage/{NODE}/{entry['storage']}",
                "type": "storage",
                "node": NODE,
                "storage": entry["storage"],
                "status": "available" if status["active"] else "unknown",
                "plugintype": entry["type"],
                "content": status["content"],
                "shared": status["shared"],
                "disk": status["used"],
                "maxdisk": status["total"],
            }
        )
    return out


def day_history(opts, cpu, memory, maxmem):
    """A day's history as --timeframe day --cf AVERAGE gives it, a point every ten minutes, the first with only its
    time."""
    if opts != {"timeframe": "day", "cf": "AVERAGE"}:
        fail(f"400 Parameter verification failed: {sorted(opts)}")
    points = [{"time": NOW - 86400}]
    for t in range(NOW - 86400 + 600, NOW + 1, 600):
        points.append({"time": t, "cpu": cpu, "maxcpu": 8, "mem": memory, "maxmem": maxmem, "netin": 1000.0})
    return points


def get_node_rrddata(state, node, opts):
    check_node(node)
    memory = state.get("node_memory", {"total": 64 * 2**30, "used": 4 * 2**30})
    return day_history(opts, 0.12, memory["used"], memory["total"])


def get_vm_rrddata(state, node, vmid, opts):
    check_node(node)
    vm = vm_of(state, vmid)
    usage = vm_usage(state, vmid, vm)
    return day_history(opts, usage["cpu"], usage["mem"], usage["maxmem"])


def get_node_storage(state, node, opts):
    check_node(node)
    no_options(opts)
    out = []
    for entry in state["storage"]:
        status = get_storage_status(state, node, entry["storage"], {})
        out.append({"storage": entry["storage"], **status})
    return out


def get_content(state, node, sid, opts):
    check_node(node)
    storage_entry(state, sid)
    unknown = set(opts) - {"content", "vmid"}
    if unknown:
        fail(f"400 Parameter verification failed.\n{sorted(unknown)[0]}: property is not defined in schema")
    volumes = state.get("content", {}).get(sid, [])
    if "content" in opts:
        volumes = [v for v in volumes if v["content"] == opts["content"]]
    if "vmid" in opts:
        # As Proxmox lists them: files that belong to no VMID (imports, snippets) match any --vmid.
        volumes = [v for v in volumes if "vmid" not in v or str(v["vmid"]) == opts["vmid"]]
    return volumes


def download_url(state, node, sid, opts):
    check_node(node)
    entry = storage_entry(state, sid)
    if set(opts) - {"content", "filename", "url", "checksum", "checksum-algorithm"} or not {
        "content",
        "filename",
        "url",
    } <= set(opts):
        fail(f"400 Parameter verification failed: {sorted(opts)}")
    if ("checksum" in opts) != ("checksum-algorithm" in opts):
        fail("400 Parameter verification failed.\nchecksum: requires option(s): checksum-algorithm")
    if opts["content"] not in entry["content"].split(","):
        fail(f"storage '{sid}' does not support content type '{opts['content']}'")
    if opts["content"] == "import" and not re.fullmatch(r"[a-zA-Z0-9\-.+=_]+\.(ova|qcow2|raw|vmdk)", opts["filename"]):
        fail(f"invalid filename or wrong extension: {opts['filename']}")
    volid = f"{sid}:{opts['content']}/{opts['filename']}"
    content = state.setdefault("content", {}).setdefault(sid, [])
    if any(v["volid"] == volid for v in content):
        fail(f"refusing to override existing file '{opts['filename']}'")
    upid = f"UPID:{NODE}:0000{len(state.get('tasks', {})):04X}:00000000:00000000:download:{opts['filename']}:root@pam:"
    # What was asked, the checksum among it: the fake has no file to check it against.
    state.setdefault("downloads", []).append(dict(opts))
    failed = state.get("download_fails")
    mismatch = "checksum" in opts and state.get("checksum_fails")
    exitstatus = "download failed: 404 Not Found" if failed else "OK"
    if mismatch and not failed:
        exitstatus = f"checksum mismatch: got '{'0' * 64}' != expect '{opts['checksum']}'"
    state.setdefault("tasks", {})[upid] = {"status": "stopped", "exitstatus": exitstatus}
    # Kept for the tests, which check what the role asked Proxmox to verify; Proxmox keeps no such record.
    state.setdefault("checksums", {})[opts["filename"]] = [opts.get("checksum-algorithm"), opts.get("checksum")]
    if exitstatus == "OK":
        fmt = opts["filename"].rsplit(".", 1)[-1]
        content.append({"volid": volid, "content": opts["content"], "size": 100 * 2**20, "format": fmt})
    state["mutations"] += 1
    # pvesh prints a worker's UPID once it has finished, and exits 0 whatever the worker's own result.
    print(upid)


def get_task_status(state, node, upid, opts):
    check_node(node)
    no_options(opts)
    task = state.get("tasks", {}).get(upid)
    if task is None:
        fail(f"no such task '{upid}'")
    return {"upid": upid, "type": "download", **task}


ROUTES = [
    (re.compile(r"/storage"), {"get": get_storage}),
    (re.compile(r"/cluster/resources"), {"get": get_cluster_resources}),
    (re.compile(r"/nodes/([^/]+)/rrddata"), {"get": get_node_rrddata}),
    (re.compile(r"/nodes/([^/]+)/qemu/(\d+)/rrddata"), {"get": get_vm_rrddata}),
    (re.compile(r"/nodes/([^/]+)/config"), {"get": get_node_config}),
    (re.compile(r"/nodes/([^/]+)/status"), {"get": get_node_status}),
    (re.compile(r"/nodes/([^/]+)/qemu"), {"get": get_qemu}),
    (re.compile(r"/nodes/([^/]+)/lxc"), {"get": get_lxc}),
    (re.compile(r"/nodes/([^/]+)/qemu/(\d+)/config"), {"get": get_vm_config}),
    (re.compile(r"/nodes/([^/]+)/qemu/(\d+)/pending"), {"get": get_vm_pending}),
    (re.compile(r"/nodes/([^/]+)/storage"), {"get": get_node_storage}),
    (re.compile(r"/nodes/([^/]+)/storage/([^/]+)/content"), {"get": get_content}),
    (re.compile(r"/nodes/([^/]+)/storage/([^/]+)/download-url"), {"create": download_url}),
    (re.compile(r"/nodes/([^/]+)/tasks/([^/]+)/status"), {"get": get_task_status}),
    (re.compile(r"/nodes/([^/]+)/tasks"), {"get": get_tasks}),
    (re.compile(r"/nodes/([^/]+)/tasks/([^/]+)/log"), {"get": get_task_log}),
    (re.compile(r"/cluster/jobs/schedule-analyze"), {"get": schedule_analyze}),
    (re.compile(r"/nodes/([^/]+)/storage/([^/]+)/status"), {"get": get_storage_status}),
    (re.compile(r"/cluster/backup"), {"get": get_backup_jobs, "create": create_backup_job}),
    (re.compile(r"/cluster/backup/([^/]+)"), {"get": get_backup_job, "set": set_backup_job}),
]


def pvesm(state, args):
    verb = args[0]
    if verb == "add":
        if args[1] != "nfs":
            fail("fake pvesm: only nfs storage is supported")
        sid, opts = args[2], options(args[3:])
        if len(sid) < 2:
            fail(f"400 Parameter verification failed.\nstorage: storage ID '{sid}' cannot be shorter than 2 characters")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*[a-z0-9]", sid, re.IGNORECASE):
            fail(f"400 Parameter verification failed.\nstorage: storage ID '{sid}' contains illegal characters")
        if any(s["storage"] == sid for s in state["storage"]):
            fail(f"create storage failed: storage ID '{sid}' already defined")
        missing = {"server", "export"} - set(opts)
        if missing:
            fail(f"400 missing option(s) {sorted(missing)}")
        entry = {"storage": sid, "type": "nfs", "path": f"/mnt/pve/{sid}", **opts}
    elif verb == "set":
        sid, opts = args[1], options(args[2:])
        entry = next((s for s in state["storage"] if s["storage"] == sid), None)
        if entry is None:
            fail(f"update storage failed: storage '{sid}' does not exist")
        state["storage"].remove(entry)
        for fixed in ("server", "export", "path", "type"):
            if fixed in opts:
                fail(f"400 option '{fixed}' can't be changed")
        for name in opts.pop("delete", "").split(","):
            entry.pop(name, None)
        entry.update(opts)
    else:
        fail(f"fake pvesm: unsupported verb {verb}")
    if entry.get("disable") == "0":
        entry.pop("disable")  # Proxmox stores and shows the flag only when it is set.
    elif entry.get("disable") == "1":
        entry["disable"] = 1
    state["storage"].append(entry)
    state["mutations"] += 1


def pvenode(state, args):
    if args[:2] != ["config", "set"]:
        fail(f"fake pvenode: unsupported {args}")
    opts = options(args[2:])
    for key, value in opts.items():
        if key != "startall-onboot-delay":
            fail(f"fake pvenode: unsupported option {key}")
        delay = int(value)
        if not 0 <= delay <= 300:
            fail("400 startall-onboot-delay: value must be between 0 and 300")
        state["node"][key] = delay
    state["mutations"] += 1


def stanzas(path):
    """The fields of each deb822 stanza in a .sources file, keys lower-cased."""
    out, current = [], {}
    for line in path.read_text().splitlines():
        if not line.strip():
            if current:
                out.append(current)
            current = {}
        elif not line.startswith("#"):
            key, _, value = line.partition(":")
            current[key.strip().lower()] = value.strip()
    if current:
        out.append(current)
    return out


def hosted_at(uris, host, path_prefix=""):
    """Whether one of the space-separated URIs is on `host`, under `path_prefix`."""
    return any(urlsplit(uri).hostname == host and urlsplit(uri).path.startswith(path_prefix) for uri in uris.split())


def repositories():
    """(uris, components, enabled) for every stanza in $FAKE_PVE_SOURCES_DIR."""
    directory = os.environ.get("FAKE_PVE_SOURCES_DIR")
    if not directory:
        fail("fake apt-get: FAKE_PVE_SOURCES_DIR is not set")
    for path in sorted(Path(directory).glob("*.sources")):
        for stanza in stanzas(path):
            enabled = stanza.get("enabled", "yes").lower() not in ("no", "false", "off", "0")
            yield stanza.get("uris", ""), stanza.get("components", "").split(), enabled


def apt_get(state, args):
    state["apt_get_runs"] = state.get("apt_get_runs", 0) + 1
    packages = state.setdefault("packages", json.loads(json.dumps(PACKAGES)))
    flags, verbs, i = set(), [], 0
    while i < len(args):
        if args[i] == "-o":  # -o Dpkg::Options::=..., accepted and ignored
            i += 2
            continue
        (flags.add if args[i].startswith("-") else verbs.append)(args[i])
        i += 1
    if verbs == ["update"]:
        found = list(repositories())
        for uris, _, enabled in found:
            if enabled and hosted_at(uris, "enterprise.proxmox.com"):
                print(f"E: Failed to fetch {uris}/dists/trixie/InRelease  401  Unauthorized", file=sys.stderr)
                print(f"E: The repository '{uris} trixie InRelease' is not signed.", file=sys.stderr)
                sys.exit(100)
        offered = any(
            enabled and hosted_at(uris, "download.proxmox.com", "/debian/pve") and "pve-no-subscription" in components
            for uris, components, enabled in found
        )
        packages["lists"] = dict(packages["repository"]) if offered else {}
        print("Reading package lists...")
    elif verbs == ["dist-upgrade"]:
        simulate = "-s" in flags
        if not simulate and ("-y" not in flags or os.environ.get("DEBIAN_FRONTEND") != "noninteractive"):
            fail("fake apt-get: dist-upgrade would stop to ask: it needs -y and DEBIAN_FRONTEND=noninteractive")
        upgrades = {p: v for p, v in packages["lists"].items() if packages["installed"].get(p) != v}
        new = sum(1 for p in upgrades if p not in packages["installed"])
        print(f"{len(upgrades) - new} upgraded, {new} newly installed, 0 to remove and 0 not upgraded.")
        for name, version in sorted(upgrades.items()):
            old = packages["installed"].get(name)
            if simulate:
                print(f"Inst {name} {f'[{old}] ' if old else ''}({version} Proxmox:trixie [amd64])")
            else:
                print(f"Unpacking {name} ({version}) {f'over ({old}) ' if old else ''}...")
        if not simulate and upgrades:
            packages["installed"].update(upgrades)
            state["mutations"] += 1
            if state.get("dpkg_fails"):
                print("Errors were encountered while processing:\n " + sorted(upgrades)[0])
                print("E: Sub-process /usr/bin/dpkg returned an error code (1)", file=sys.stderr)
                save(state)
                sys.exit(100)
    else:
        fail(f"fake apt-get: unsupported {args}")


def systemctl(state, args):
    if args != ["daemon-reload"]:
        fail(f"fake systemctl: unsupported {args}")
    state["daemon_reloads"] = state.get("daemon_reloads", 0) + 1


def hostname(state, args):
    if args != ["-s"]:
        fail(f"fake hostname: unsupported {args}")
    print(NODE)


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    tools = {
        "pvesh": pvesh,
        "pvesm": pvesm,
        "qm": qm,
        "pvenode": pvenode,
        "hostname": hostname,
        "systemctl": systemctl,
        "apt-get": apt_get,
    }
    tools[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
